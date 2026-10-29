"""档案页 / 学期侧写页共用的底层件（v1.9.2）。

v1.9.2 把档案页改成了**两级视图**：左边学生名单、右边详情；详情里点学期时间轴
再进「学期侧写」页。两页要共用同一批东西，所以拆出这一层：

* 纯逻辑 helper（成绩行归一、学期分组、提示词拼装、文件名清洗……）——
  **零 Qt**，pytest 可以直接测；
* 名单委托的配色与 Qt 角色常量（``ui/pages/archive_page.py`` 也在用）；
* **学生主档适配层** :class:`StudentBook` —— ``core.database.StudentStore`` 的薄封装。
  ★ 主档由另一位开发员维护，契约可能是分批落地的：**读不到就走内存假实现并打
  warning**，绝不因为主档没接上就把整个档案页打不开（那等于 v1.9.1 的功能一起没了）；
* 学期侧写的读写（优先 ``ProfileStore`` 的新接口，兜底
  ``semester_side/<学号>/<学期>.json.enc``，仍然**只落密文**）；
* 学期均分 / 加权均分 / GPA 的**唯一**计算入口 —— 一律转发
  :mod:`core.calculator` / :mod:`core.gpa`，本文件里**没有第二份加权算术**。

★ 本模块**不许**出现 ``QGraphicsOpacityEffect`` / ``QGraphicsDropShadowEffect``
（AGENTS §4a / §4b / §17）：页面一动效果就整页离屏重绘，字全换画法。
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.archival import SEMESTER_ORDER, merge_records, partition, semester_rank, trend
from core.calculator import (
    CalculationScope,
    CourseScore,
    filter_by_scope,
    weighted_average,
)
from core.crypto_store import CryptoError, CryptoStore
from core.database import GradeHistoryDB, ProfileStore
from core.gpa import DEFAULT_SCHEME, gpa_of
from core.parser import Category
from core.scholarship import list_classes, load_moral, save_moral
from ui.intentions import INTENTION_OPTIONS, KEY_PREFIX_NAME, KEY_PREFIX_SID, student_key
from core.user_store import (
    delete_user_json,
    load_user_json,
    save_user_json,
    user_file,
)# ---------------------------------------------------------------------------
# 常量（从 v1.9.1 的 profile_page.py 原地搬过来，一个值都没动）
# ---------------------------------------------------------------------------

#: "提一提"的目标分（Spec 六：附一句"提至85可+0.X"）
RESCUE_TARGET = 85.0

#: 趋势判定的死区：|Δ| 小于这个值就当"平稳"
TREND_EPS = 0.5

#: 可抢救项最多列几门（列表太长反而看不清重点）
MAX_RESCUE = 5

#: 左侧名单宽度
LIST_WIDTH = 236

#: 名单里一行的高度。要同时放得下头像圆(34) + 上下各 8px 呼吸，还要让
#: 两行字（姓名 13px / 学号 11px）不挤在一起。
ITEM_HEIGHT = 54

#: 头像圆的直径
_AVATAR = 34

#: 右侧第 1 个标签页的表头（四列：课程 / 成绩 / 学分 / 操作）
#: ★ 第 4 列是「操作」：里面那个「删除」是 :class:`RowActionDelegate` 画出来的，
#:   不是 `setCellWidget` —— 委托画的按钮才能跟着滚动走、也不吃内存。
GRADE_TREE_COLUMNS = ("课程", "成绩", "学分", "操作")

#: 「操作」列里那个小按钮的文字。**纯文字，不许用 emoji**（§4l：emoji 是启动开销）
ACTION_TEXT = "删除"

#: 没有侧写正文时显示的那句话（判据：`test_profile_merge.py` 认这一串）
SIDE_TEXT_EMPTY_HINT = "还没有侧写。点右上角的「AI 侧写」生成。"

#: 单条删除的确认框正文。**逐字**照任务书，一个字都不许改。
DELETE_CONFIRM_TEXT = "确定删除此条记录？此操作不可恢复。"

#: 删掉某学期最后一条成绩之后的追加提示（任务书 3）。
DELETE_TERM_PROMPT_TEXT = "该学期已无成绩，是否同时删除学期记录？"

#: 学生数据缺失时的占位文字（写进提示词的 JSON 里）。
NO_MORAL_TEXT = "未录入"

#: AI 辅导页的系统提示词模板。``{学生JSON}`` 由
#: :func:`build_student_json` 的结果逐字替换进去 —— **一个字都不许改**。
CHAT_SYSTEM_PROMPT_TEMPLATE = (
    "你是一名高校辅导员助理，正在协助辅导员与一名学生进行谈话。"
    "以下是该学生的数据：{学生JSON}。"
    "请根据这些数据提供谈话建议、侧写分析或评语润色。"
)

#: 谈话记录落在用户目录下的哪个子目录（任务书 2 指定的
#: ``u_<uid>\cache\chat_history_<学号>.json.enc``）。目录不用自己建 ——
#: ``core.crypto_store._atomic_write_bytes`` 落盘前会 ``mkdir(parents=True)``。
CHAT_HISTORY_DIR = "cache/"

#: 谈话记录落盘的文件名前缀 / 后缀。★ **只收 ``.enc``**：
#: ``save_user_json`` 见到 ``.json`` 会当场拒绝（AGENTS §32 踩过），
#: 而且磁盘上绝不许出现明文谈话记录（红线 3）。
CHAT_HISTORY_PREFIX = "chat_history_"
CHAT_HISTORY_SUFFIX = ".json.enc"

#: 谈话记录载荷的结构版本（将来改结构靠它迁移）。
CHAT_HISTORY_SCHEMA = 1

#: 谈话记录里两种角色的**存储**键（英文，结构稳定；显示才翻成中文）。
CHAT_ROLE_USER = "user"
CHAT_ROLE_ASSISTANT = "assistant"

#: 拼提示词时最多带最近多少轮（多了会撑爆 n_ctx=2048 的上下文窗口）。
CHAT_CONTEXT_TURNS = 12

#: 提示词的字符上限（双保险：一轮很长时按字符再裁一次）。
CHAT_MAX_PROMPT_CHARS = 4000

#: 生成时的停止串：模型一旦开始"扮演辅导员"就停。
CHAT_STOP_WORDS = ("\n辅导员：", "辅导员：")

#: 文件名里不允许出现的字符（学号里的 ``:`` / 空格 / 引号都换掉）。
_UNSAFE_FILENAME = re.compile(r"[^0-9A-Za-z_-]+")


# ---------------------------------------------------------------------------
# 纯逻辑 helper（零 Qt；v1.9.1 的 profile_page.py 里原样搬来）
# ---------------------------------------------------------------------------

def safe_student_file(key: str) -> str:
    """把 ``sid:2023001`` 这种键变成安全的文件名片段。

    ★ 谈话记录按学生分开存（``cache/chat_history_<学号>.json.enc``），而键里带
      ``sid:`` 前缀 —— 冒号在 Windows 上根本不是合法文件名字符。没有学号时
      退回"整个键的清洗结果"，**绝不返回空串**（空串会拼出
      ``chat_history_.json.enc``，两个没学号的学生就共用一份记录了）。
    """
    text = str(key or "").strip()
    for prefix in ("sid:", "name:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    safe = _UNSAFE_FILENAME.sub("_", text).strip("_")
    if not safe:
        safe = "unknown"
    return safe[:64]


def moral_text(value: Any) -> str:
    """德育分那一行的文字。**没录 ≠ 0 分**（AGENTS §14）：没录就写"未录入"。"""
    if value is None:
        return NO_MORAL_TEXT
    if isinstance(value, bool):          # True/False 不该被当成 1/0
        return NO_MORAL_TEXT
    if isinstance(value, (int, float)):
        return f"{value:g}"
    text = str(value).strip()
    return text or NO_MORAL_TEXT


def build_student_json(student_id: Any, name: Any, class_name: Any, grades: Any,
                       moral: Any, side_text: Any) -> str:
    """把一名学生的数据拼成**一段 JSON**（喂给提示词的 ``{学生JSON}``）。

    ★ 必须是**合法 JSON**：模型看到的是它，测试也解析它（``json.loads``）。
    ★ 只用这名学生**自己的**数据 —— 姓名 / 学号确实在提示词里，但那是
      本机离线推理的输入（红线 3 管的是"落盘"和"上传"），不是发给谁。
    """
    courses: list[dict[str, Any]] = []
    for row in grades or ():
        if not isinstance(row, dict):
            continue
        courses.append({
            "semester": str(row.get("term") or row.get("semester") or ""),
            "course": str(row.get("course") or row.get("course_name") or ""),
            # 空 ≠ 0：没考的课留在 JSON 里是 null，不是 0
            "score": row.get("score"),
            "credit": row.get("credit"),
        })
    payload = {
        "学号": str(student_id or ""),
        "姓名": str(name or ""),
        "班级": str(class_name or ""),
        "德育分": None if moral is None else moral,
        "成绩": courses,
        "已有侧写": str(side_text or ""),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def build_chat_system_prompt(student_json: str) -> str:
    """把学生 JSON 逐字塞进系统提示词模板。"""
    return CHAT_SYSTEM_PROMPT_TEMPLATE.replace("{学生JSON}", student_json)


def store_archive(store: Any, username: str, archive: Any) -> bool:
    """把归档写回**应用级**加密库（``archive.json.enc``）。

    ★ 归档走应用级（AGENTS §16 / §20c）：自动登录时没有密码，库密钥解不开；
      成绩绝不许往应用级里塞，但归档是"导入时算过一遍"的派生数据，属于允许的一侧。
    Returns:
        True 表示真的写下去了；False 表示这次没写（**不抛**）。
    """
    if archive is None or not username:
        return False
    try:
        from core.user_store import save_user_json

        payload = archive.as_dict() if hasattr(archive, "as_dict") else archive
        save_user_json(store, username, "archive.json.enc", payload)
    except Exception:  # noqa: BLE001 - 存不下只记日志，界面那一份还在
        logger.exception("写归档失败")
        return False
    return True


def build_chat_prompt(system_prompt: str, turns: Any) -> str:
    """系统提示词 + 最近若干轮 → 一段纯文本提示词。

    写法照 ``ui/chat_dialog.py`` 的 ``_build_prompt``（那边是冻结的参考实现）：
    最后一轮一定是"辅导员刚说的话"，模型接着往下答。
    """
    items = [turn for turn in (turns or ()) if isinstance(turn, dict)]
    recent = items[-(CHAT_CONTEXT_TURNS + 1): -1] if items else []
    lines = [system_prompt, "", "下面是谈话记录（辅导员 = 用户，你 = 助手）："]
    body: list[str] = []
    for turn in recent:
        speaker = "辅导员" if turn.get("role") == CHAT_ROLE_USER else "你"
        body.append(f"{speaker}：{turn.get('text', '')}")
    if items:
        body.append(f"辅导员：{items[-1].get('text', '')}")
    blob = "\n".join(body)
    if len(blob) > CHAT_MAX_PROMPT_CHARS:
        blob = blob[-CHAT_MAX_PROMPT_CHARS:]
        blob = blob.split("\n", 1)[-1]
    lines.append(blob)
    lines.append("")
    lines.append("请用中文、分点回答，不要输出 Markdown 记号。")
    lines.append("你：")
    return "\n".join(lines)


def format_score(score: Any) -> str:
    """成绩列的文字。

    ★ **空 ≠ 0**（红线 2）：没选 / 没考的课显示**空白**，绝不显示 ``0``。
    """
    if score is None or score == "":
        return ""
    try:
        return f"{float(score):g}"
    except (TypeError, ValueError):
        return str(score)


def format_credit(credit: Any) -> str:
    """学分列的文字。缺失（``None``）同样显示**空白**，不是 0。"""
    if credit is None or credit == "":
        return ""
    try:
        return f"{float(credit):g}"
    except (TypeError, ValueError):
        return str(credit)


def term_of_archive(archive: Any) -> str:
    """归档里**最近一个**学期的标签（用来给没有 term 的明细行标学期）。

    取 ``_ordered_records`` 的最后一个 —— 那是 order 最大的学期，
    也就是"最近一次导入的学期"。
    """
    records = _ordered_records(archive)
    return str(getattr(records[-1], "term", "") or "") if records else ""


def student_archive_of(archive: Any, key: str) -> Any:
    """从"可能是整份归档、也可能就是这一名学生自己的归档"里取出这一名学生的归档。

    ★ 两种形状都要认：主窗给的是 ``Archive``（要按 ``key`` 取），而档案页 /
      测试给的常常就是 :class:`core.archival.StudentArchive` 本身。只认一种的
      症状是 ``AttributeError: 'StudentArchive' object has no attribute 'get'``。
    """
    if archive is None:
        return None
    if hasattr(archive, "records"):
        return archive
    getter = getattr(archive, "get", None)
    if not callable(getter):
        return None
    try:
        return getter(key)
    except Exception:  # noqa: BLE001 - 取不出来就当这名学生没有归档
        logger.exception("从归档里取学生失败：%s", key)
        return None


def _ordered_records(archive: Any) -> list[Any]:
    """归档里这个学生的学期记录，按 ``core.archival`` 的顺序。

    ★ 学期顺序**必须**来自 :mod:`core.archival`（``sorted_records`` 用的是
      ``semester_rank`` / ``SEMESTER_ORDER``）—— 本页不另抄一份表，
      抄了就会漂移，症状是"大二下排在大一上前头"。
    """
    if archive is None:
        return []
    ordered = getattr(archive, "sorted_records", None)
    if callable(ordered):
        try:
            return list(ordered())
        except Exception:  # noqa: BLE001 - 坏档案不该拦住看明细
            pass
    records = list(getattr(archive, "records", ()) or ())
    return sorted(records, key=lambda rec: semester_rank(getattr(rec, "term", "")))


def grade_rows_from_records(archive: Any) -> list[dict[str, Any]]:
    """归档 → ``[{"term": ..., "course": ..., "score": ..., "credit": ...}]``。

    只搬运，不改口径：``score is None``（没选这门课）原样留着，
    由 :func:`format_score` 决定显示成空白。
    """
    rows: list[dict[str, Any]] = []
    for record in _ordered_records(archive):
        term = str(getattr(record, "term", "") or "")
        for course in getattr(record, "courses", ()) or ():
            rows.append({
                "term": term,
                "course": str(getattr(course, "name", "") or ""),
                "score": getattr(course, "score", None),
                "credit": getattr(course, "credit", None),
                "category": str(getattr(course, "category", "") or ""),
            })
    return rows


def group_grades_by_term(rows: list[dict[str, Any]],
                         terminal_terms: Any = None) -> list[tuple[str, list[dict[str, Any]]]]:
    """按学期分组，并把学期排成时间顺序。

    参数：
        rows: :func:`grade_rows_from_records` 那种行；也接受 ``core.database``
            的原始行（键是 ``course_name`` / ``score`` / ``credit``）
        terminal_terms: **已终结**的学期（转成 ``set`` 再传进来最省）。
            归档记录本身带的 ``term`` 就是终结学期；成绩历史表的行没有这个
            概念，所以只有调用方显式喂进来时才用得上。

    规则：
        * 终结学期**永远排在最前**（和 :func:`core.archival.merge_records`
          的既有行为一致，不再另立一套）；
        * 其余学期按 ``SEMESTER_ORDER`` 的时间顺序；认不出的排在最后；
        * 同一个学期里，有分数的排前面（高的在前），没分数的排最后、按课程名；
        * 课程名为空的（终结学期里的占位）单独归一堆，名字统一成
          「（历史课程名缺失）」。

    返回 ``[(学期, [行, ...]), ...]``，学期已去重且排好序。
    """
    terminal = {str(t or "").strip() for t in (terminal_terms or ())}
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        term = str(row.get("term") or row.get("semester") or "").strip()
        course = str(row.get("course") or row.get("course_name") or "").strip()
        if not course:
            if term not in terminal:
                # 非终结学期里课程名不该为空 —— 空课程名无法按主键去重，
                # 也就无从显示，直接跳过（不编一个假名字出来）。
                continue
            course = "（历史课程名缺失）"
        buckets.setdefault(term, []).append({
            "term": term,
            "course": course,
            "score": row.get("score", None),
            "credit": row.get("credit", None),
            "category": str(row.get("category") or ""),
        })

    def rank(term: str) -> tuple[int, int, str]:
        known = term in SEMESTER_ORDER
        return (
            0 if term in terminal else 1,
            0 if known else 1,
            f"{semester_rank(term):03d}" if known else term,
        )

    ordered: list[tuple[str, list[dict[str, Any]]]] = []
    for term in sorted(buckets, key=rank):
        courses = buckets[term]
        courses.sort(key=lambda row: (
            row["score"] is None,
            -(row["score"] if isinstance(row["score"], (int, float)) else 0.0),
            row["course"],
        ))
        ordered.append((term, courses))
    return ordered


def moral_of(store: CryptoStore, username: str, key: str,
             class_names: Any = None) -> int | None:
    """在某一个班里找这名学生的德育分。

    ★ **没录 ≠ 0**（红线 2）：找不到就返回 ``None``，界面把它显示成空白。
      这也是为什么这个函数不返回 ``0`` —— 0 分和"没录"在奖学金那一页
      是两种完全不同的待遇（备注里必须写"德育分未录入，按 0 计"）。

    ``class_names`` 给了就只查那些班（省一次扫目录）；没给就 :func:`list_classes`。
    """
    if not username or not key:
        return None
    if class_names is None:
        try:
            class_names = [record.class_name for record in list_classes(store, username)]
        except (CryptoError, ValueError, OSError):
            return None
    for name in class_names:
        try:
            scores = load_moral(store, username, name)
        except (CryptoError, ValueError, OSError):
            continue
        if key in scores:
            return scores[key]
    return None


def build_side_text_prompt(student: str, student_id: str, key: str, grades: Any,
                           moral: Any = None) -> str:
    """把"已有的成绩与德育分"拼成一段提示词，交给 AI 谈话助手。

    ★ **只拼已有字段，不做任何算术**（铁律五）：平均分 / 加权 / 排名一概不
      在这里算，也一概不往里写 —— 提示词里出现一个本页算出来的数字，
      就等于绕开了"算术只在 core 里做"这条规矩。
    ★ 键一律用 ``sid:<学号>``；没有学号才退回 ``name:<姓名>``，
      并且**明说同名风险**（AGENTS §9）。
    """
    lines = [
        "请根据下面这名学生已有的成绩，写一段学业侧写（优势、短板、"
        "下学期可以怎么补），不要编造没有出现的数字。",
        "",
        f"学生：{student or '（未填）'}",
        f"学号：{student_id.strip() or '（成绩表里没有学号）'}",
    ]
    if key.startswith(KEY_PREFIX_NAME):
        lines.append("注意：这名学生没有学号，若班上有同名的人，不要把他人的成绩算进来。")
    lines.extend(["", "已有成绩（只列已导入的字段，空白＝没选或没考）："])
    grouped = group_grades_by_term(list(grades or ()))
    if grouped:
        for term, courses in grouped:
            lines.append(f"【{term or '未标注学期'}】")
            for row in courses:
                score = format_score(row["score"])
                credit = format_credit(row["credit"])
                detail = f"成绩 {score}" if score else "成绩（空白）"
                detail += f"　学分 {credit}" if credit else "　学分（空白）"
                lines.append(f"· {row['course']}　{detail}")
    else:
        lines.append("（还没有导入任何成绩明细）")
    if moral is None:
        lines.append("")
        lines.append("德育分：未录入")
    else:
        lines.extend(["", f"德育分：{moral}"])
    return "\n".join(lines)


def read_side_text(store: Any, student_id: str, key: str) -> str:
    """读侧写正文（``ProfileStore.side_text``）。

    读不到（没登录 / 库坏了 / 这个学生没存过）一律返回空串 ——
    侧写是便利数据，坏掉不该拦住看成绩。
    """
    sid = (key or student_id or "").strip()
    if not sid or store is None:
        return ""
    getter = getattr(store, "side_text", None)
    if not callable(getter):
        return ""
    try:
        return getter(sid) or ""
    except Exception:  # noqa: BLE001 - 读不出来就当没有
        return ""



# ---------------------------------------------------------------------------
# 名单条目上挂的数据 + 委托配色（archive_page 也在用）
# ---------------------------------------------------------------------------

# ---- 条目上挂的数据（delegate 绘制时读）-----------------------------------
_ROLE_INDEX = Qt.ItemDataRole.UserRole          #: 行号（原有，别改）
_ROLE_NAME = Qt.ItemDataRole.UserRole + 1       #: 姓名
_ROLE_ID = Qt.ItemDataRole.UserRole + 2         #: 学号（可能是空串）
_ROLE_HAS_ID = Qt.ItemDataRole.UserRole + 3     #: 有没有学号
_ROLE_LEVEL = Qt.ItemDataRole.UserRole + 4      #: "" / "danger" / "warning"
_ROLE_SEED = Qt.ItemDataRole.UserRole + 5       #: 头像配色的稳定种子

#: 头像底色。按学号取模选一个 —— 同一个人每次打开都是同一个颜色。
#:
#: ★ 绝不能用内置 ``hash()`` 当种子：Python 对 str 的 hash **每个进程都加盐**，
#:   同一个人这次是蓝的、下次开程序就变绿了，看着像换了个人。
_AVATAR_COLORS = (
    "#3B82F6", "#0EA5E9", "#8B5CF6", "#EC4899",
    "#F59E0B", "#10B981", "#6366F1", "#14B8A6",
)

_SEL_BG = "#EFF6FF"
_HOVER_BG = "#F8FAFC"
_ACCENT = "#3B82F6"
_TEXT = "#0F172A"
_MUTED = "#94A3B8"

_DANGER = "#EF4444"
_WARNING = "#F59E0B"


def _avatar_color(seed: str) -> str:
    """稳定的头像配色。**不用 hash()**（见 _AVATAR_COLORS 的注释）。"""
    if not seed:
        return "#94A3B8"
    return _AVATAR_COLORS[sum(ord(ch) for ch in seed) % len(_AVATAR_COLORS)]


def _initial(name: str) -> str:
    """头像里那个字：中文取姓氏，英文取首字母。"""
    text = (name or "").strip()
    return text[0].upper() if text else "?"


def _fmt(value: float | None, digits: int = 2) -> str:
    """分数/均分统一两位（``None`` → 「—」，**不是 0.00**）。"""
    return "—" if value is None else f"{value:.{digits}f}"


# ---------------------------------------------------------------------------
# 日志与"契约降级"的公共设施
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

#: 一次性 warning 的键（同一个缺失只吵一次，别把日志刷满）
_WARNED: set[str] = set()


def warn_once(key: str, message: str, *args: Any) -> None:
    """同一个键只 warning 一次（缺契约时的降级提示不该每刷一次名单就刷一条）。"""
    if key in _WARNED:
        return
    _WARNED.add(key)
    logger.warning(message, *args)


# ---------------------------------------------------------------------------
# 学生主档（v1.9.2 §37）
# ---------------------------------------------------------------------------
#: 主档里一条记录的字段（与 ``core.database.STUDENT_FIELDS`` 同口径）
STUDENT_RECORD_FIELDS = (
    "student_id",
    "name",
    "class_name",
    "note",
    "archived",
    "source",
)

#: 导入补齐时写进 ``source`` 的标记（主档要知道这条是怎么来的）
STUDENT_SOURCE_IMPORT = "import"

#: 手工新建 / 编辑时的来源标记
STUDENT_SOURCE_MANUAL = "manual"

#: 搜索框里输这个词 = 只看归档（任务书第 5 节）
ARCHIVED_SEARCH_TEXT = "已归档"


class DuplicateStudentError(Exception):
    """学号重复。UI 必须弹 ``critical`` 并且**一个字节都不许写盘**。"""

    def __init__(self, student_id: str) -> None:
        self.student_id = student_id
        super().__init__(f"学号 {student_id} 已存在，不能重复录入")


@dataclass
class StudentRecord:
    """学生主档的一行（``students.json.enc`` 的逻辑视图）。"""

    student_id: str = ""
    name: str = ""
    class_name: str = ""
    note: str = ""
    archived: bool = False
    source: str = ""

    @property
    def key(self) -> str:
        """稳定标识（有学号走 ``sid:``，没有才退回 ``name:``，AGENTS §9）。"""
        return student_key(self.student_id, self.name)

    @property
    def label(self) -> str:
        return (
            f"{self.name}（{self.student_id}）" if self.student_id else self.name
        )


def _record_attr(record: Any, name: str, default: Any = "") -> Any:
    """从 dict / 对象里取字段（主档实现可能用 dataclass，也可能直接回 dict）。"""
    if record is None:
        return default
    if isinstance(record, dict):
        value = record.get(name, default)
    else:
        value = getattr(record, name, default)
    return default if value is None else value


def normalise_student_record(record: Any) -> StudentRecord:
    """把主档返回的东西归一成 :class:`StudentRecord`。"""
    if isinstance(record, StudentRecord):
        return record
    return StudentRecord(
        student_id=_bare_sid(_record_attr(record, "student_id", "")),
        name=str(_record_attr(record, "name", "") or "").strip(),
        class_name=str(_record_attr(record, "class_name", "") or "").strip(),
        note=str(_record_attr(record, "note", "") or ""),
        archived=bool(_record_attr(record, "archived", False)),
        source=str(_record_attr(record, "source", "") or ""),
    )


def same_student_id(value: Any, key: str) -> bool:
    """``成绩历史里的裸学号`` 与 ``sid: 前缀的键`` 是不是同一个学生。

    ★ 这两种形式全仓库都在用（:class:`core.database.GradeHistoryDB` 存裸学号，
      :func:`core.archival.archive_key` 给 ``sid:`` 前缀）。老代码里
      ``row["student_id"] == key`` 的写法永远不相等，于是"成绩明细"只靠归档撑着
      —— 归档一空就整页空白。这里两种形式都认。
    """
    raw = str(value or "").strip()
    if not raw:
        return False
    wanted = str(key or "").strip()
    if not wanted:
        return False
    if raw == wanted:
        return True
    return f"{KEY_PREFIX_SID}{raw}" == wanted or raw == wanted.removeprefix(KEY_PREFIX_SID)


def _core_student_store_class() -> Any | None:
    """拿 ``core.database.StudentStore``；契约还没落地时返回 ``None``。"""
    try:
        from core.database import StudentStore
    except Exception:  # noqa: BLE001 - 契约可能分批落地（__all__ 先有、类还没写）
        return None
    return StudentStore


_MISSING = object()


def _call_first(target: Any, names: Sequence[str], variants: Sequence[tuple]) -> Any:
    """按顺序试 ``(方法名, 参数形态)``；一个都不匹配返回 ``_MISSING``。

    ★ 为什么要把参数形态也列出来：主档是另一位开发员写的，方法名按契约来，但
      "位置参数还是关键字参数"没写死在契约里。这里两种都试，**试不中不写任何东西**。
    """
    for name in names:
        method = getattr(target, name, None)
        if not callable(method):
            continue
        for args, kwargs in variants:
            try:
                return method(*args, **kwargs)
            except TypeError:
                continue
    return _MISSING


_SID_PREFIX = "sid:"


def _bare_sid(value: Any) -> str:
    """去掉 ``sid:`` 前缀（主档内部存的是键形式，界面上一律显示裸学号）。"""
    text = str(value or "").strip()
    if text[:len(_SID_PREFIX)].lower() == _SID_PREFIX:
        return text[len(_SID_PREFIX):].strip()
    return text


def _is_duplicate_error(exc: BaseException) -> bool:
    return type(exc).__name__ == "DuplicateStudentError"


class StudentBook:
    """学生主档的读写口（``core.database.StudentStore`` 的薄封装）。

    Args:
        store: :class:`core.crypto_store.CryptoStore`（也可以是测试里的临时库）。
        username: 当前登录名（主档按用户分目录存）。

    ★ **降级策略**：主档类还不存在（契约未落地）时，本对象走**内存假实现**并
      :func:`warn_once` 吵一次 —— 界面照常能新建 / 编辑 / 删除，只是这一次登录
      里不落盘。**绝不**因为主档没接上就让档案页打不开。

    ★ **学号形式**：主档里存的是 ``sid:2023001``（供成绩历史/意愿共用），本对象
      对外一律给**裸学号**（``2023001``）—— 界面显示、比较、写文件名都用裸的。
    """

    def __init__(self, store: Any = None, username: str = "") -> None:
        self._store = store
        self._username = username or ""
        self._real: Any = None
        self._memory: dict[str, StudentRecord] = {}
        self._durable = False
        cls = _core_student_store_class()
        if cls is None:
            warn_once(
                "student_store_missing",
                "学生主档还没落地（core.database.StudentStore 缺失），"
                "本次运行使用内存假实现：新建 / 编辑 / 删除只在当前页面有效，不落盘。",
            )
            return
        try:
            self._real = _open_student_store(cls, store, self._username)
        except Exception:  # noqa: BLE001 - 主档坏了也不能把档案页带崩
            logger.warning("初始化 StudentStore 失败，改用内存假实现", exc_info=True)
            self._real = None
            return
        self._durable = self._real is not None
        self.refresh()

    # ------------------------------------------------------------------ 读
    @property
    def durable(self) -> bool:
        """``True`` = 真的挂在主档上；``False`` = 内存假实现。"""
        return self._durable

    @property
    def available(self) -> bool:
        """主档类存在（无论现在能不能读）。"""
        return _core_student_store_class() is not None

    def refresh(self) -> None:
        """从主档重读一遍（切账户、导完一批之后调用）。"""
        if self._real is None:
            return
        rows = self._read_all_from_real()
        if rows is None:
            return
        merged: dict[str, StudentRecord] = {}
        for raw in rows:
            record = normalise_student_record(raw)
            if record.student_id:
                merged[record.student_id] = record
        # 主档还读不出东西时，保留本进程里刚写的那几条（契约可能"写得进读不出"）
        for sid, record in self._memory.items():
            merged.setdefault(sid, record)
        self._memory = merged

    def _read_all_from_real(self) -> list[Any] | None:
        """整张表（**含归档**）。优先 ``all_rows()`` —— ``rows()`` 默认不含归档。"""
        rows = _call_first(self._real, ("all_rows", "list_all"), (((), {}),))
        if rows is _MISSING:
            rows = _call_first(
                self._real, ("rows",), (((), {"include_archived": True}),)
            )
        if rows is _MISSING or rows is None:
            return None
        try:
            return list(rows)
        except TypeError:
            return None

    def records(self, *, include_archived: bool = True) -> list[StudentRecord]:
        """全部记录（按学号、姓名排；**空学号的记录不会被主档保存**）。"""
        self.refresh()
        values = list(self._memory.values())
        if not include_archived:
            values = [record for record in values if not record.archived]
        return sorted(values, key=lambda r: (r.student_id or "", r.name or ""))

    def search(self, keyword: str = "", *, archived_only: bool = False) -> list[StudentRecord]:
        """按关键字过滤（姓名 / 学号 / 班级 / 备注）。

        ★ 优先问主档自己的 ``search``（任务书要求"调 StudentStore.search"）。
        ★ 归档可见性由本方法兜底，**不赌**主档那边的语义：
          ``archived_only=False`` → 只出在册的；``archived_only=True`` → 只出归档的。
        """
        text = (keyword or "").strip()
        wanted_archived = bool(archived_only)
        needle = text.replace(ARCHIVED_SEARCH_TEXT, "").strip()

        rows: Any = _MISSING
        if self._real is not None:
            rows = _call_first(
                self._real,
                ("search",),
                (
                    ((text,), {"include_archived": True}),
                    ((text,), {}),
                    ((), {"text": text, "include_archived": True}),
                ),
            )
        if rows is _MISSING or rows is None:
            source = list(self._memory.values())
        else:
            try:
                source = [normalise_student_record(raw) for raw in rows]
            except TypeError:
                source = list(self._memory.values())

        result: list[StudentRecord] = []
        for record in source:
            if bool(record.archived) != wanted_archived:
                continue
            if needle and not self._matches(record, needle):
                continue
            result.append(record)
        result.sort(key=lambda r: (r.student_id or "", r.name or ""))
        return result

    @staticmethod
    def _matches(record: StudentRecord, text: str) -> bool:
        needle = text.casefold()
        haystack = " ".join(
            (record.name, record.student_id, record.class_name, record.note)
        ).casefold()
        return needle in haystack

    def get(self, student_id: str) -> StudentRecord | None:
        sid = _bare_sid(student_id)
        if not sid:
            return None
        return self._memory.get(sid)

    def has(self, student_id: str) -> bool:
        return self.get(student_id) is not None

    def count(self, *, include_archived: bool = True) -> int:
        return len(self.records(include_archived=include_archived))

    # ------------------------------------------------------------------ 写
    def add(
        self,
        *,
        student_id: str,
        name: str,
        class_name: str = "",
        note: str = "",
    ) -> StudentRecord:
        """新建一条。学号重复抛 :class:`DuplicateStudentError`（**不写盘**）。"""
        sid = _bare_sid(student_id)
        display = (name or "").strip()
        if not sid:
            raise ValueError("学号不能为空")
        if not display:
            raise ValueError("姓名不能为空")
        if sid in self._memory:
            raise DuplicateStudentError(sid)
        record = StudentRecord(
            student_id=sid,
            name=display,
            class_name=(class_name or "").strip(),
            note=(note or "").strip(),
            archived=False,
            source=STUDENT_SOURCE_MANUAL,
        )
        if self._real is not None:
            try:
                result = self._real.add(
                    student_id=sid,
                    name=display,
                    class_name=record.class_name,
                    note=record.note,
                    source=STUDENT_SOURCE_MANUAL,
                )
            except Exception as exc:  # noqa: BLE001
                if _is_duplicate_error(exc):
                    raise DuplicateStudentError(sid) from exc
                raise
            if isinstance(result, bool) and result is False:
                raise DuplicateStudentError(sid)
        self._memory[sid] = record
        return record

    def update(
        self,
        old_id: str,
        *,
        student_id: str,
        name: str,
        class_name: str = "",
        note: str = "",
    ) -> StudentRecord:
        """改一条（学号可能变）。

        ★ 改学号时**调用方先决定要不要迁历史数据**：迁了就调
          :func:`migrate_student_id_for`（它会连主档一起改名）；没迁就只改这里的
          主档行 —— 两种情况下主档的学号都得变成新的，所以这里备了 ``rename`` 兜底。
        """
        old = _bare_sid(old_id)
        new = _bare_sid(student_id)
        display = (name or "").strip()
        if not new:
            raise ValueError("学号不能为空")
        if not display:
            raise ValueError("姓名不能为空")
        if new != old and new in self._memory:
            raise DuplicateStudentError(new)
        previous = self._memory.get(old)
        record = StudentRecord(
            student_id=new,
            name=display,
            class_name=(class_name or "").strip(),
            note=(note or "").strip(),
            archived=bool(previous.archived) if previous is not None else False,
            source=previous.source if previous is not None else STUDENT_SOURCE_MANUAL,
        )
        if self._real is not None:
            if old and new != old and self.has_real(old):
                try:
                    self._real.rename(old, new)
                except Exception as exc:  # noqa: BLE001
                    if _is_duplicate_error(exc):
                        raise DuplicateStudentError(new) from exc
                    logger.warning("主档改名失败（%s → %s）", old, new, exc_info=True)
            try:
                self._real.update(
                    new,
                    name=record.name,
                    class_name=record.class_name,
                    note=record.note,
                )
            except KeyError:
                # 主档里没有这一行（例如改名没成功）→ 补建
                try:
                    self._real.add(
                        student_id=new,
                        name=record.name,
                        class_name=record.class_name,
                        note=record.note,
                        source=record.source,
                    )
                except Exception:  # noqa: BLE001 - 补建失败也要保住内存那份
                    logger.warning("主档补建失败（%s）", new, exc_info=True)
            except Exception as exc:  # noqa: BLE001
                if _is_duplicate_error(exc):
                    raise DuplicateStudentError(new) from exc
                logger.warning("主档更新失败（%s）", new, exc_info=True)
        if old and old != new:
            self._memory.pop(old, None)
        self._memory[new] = record
        return record

    def has_real(self, student_id: str) -> bool:
        """主档里到底有没有这一行（内存那份不算数）。"""
        if self._real is None:
            return False
        try:
            return bool(self._real.has(student_id))
        except Exception:  # noqa: BLE001
            return False

    def remove(self, student_id: str) -> bool:
        """删主档里的一条（**关联数据由调用方先清**）。"""
        sid = _bare_sid(student_id)
        if not sid:
            return False
        removed = self._memory.pop(sid, None) is not None
        if self._real is not None:
            try:
                if self._real.remove(sid):
                    removed = True
            except Exception:  # noqa: BLE001
                logger.warning("主档删除失败（%s）", sid, exc_info=True)
        return removed

    def archive(self, student_id: str, archived: bool = True) -> bool:
        """归档 / 取消归档（**数据全留**，只是名单默认不列）。"""
        sid = _bare_sid(student_id)
        record = self._memory.get(sid)
        if record is None:
            return False
        record.archived = bool(archived)
        if self._real is not None:
            try:
                if archived:
                    self._real.archive(sid)
                else:
                    self._real.restore(sid)
            except Exception:  # noqa: BLE001
                logger.warning("主档归档切换失败（%s）", sid, exc_info=True)
        return True

    def upsert_import(self, student_id: str, name: str, class_name: str = "") -> StudentRecord | None:
        """导入完成后补档（**有学号的才补**；无学号的学生只显示不落档）。"""
        sid = _bare_sid(student_id)
        if not sid:
            return None
        display = (name or "").strip()
        cleaned_class = (class_name or "").strip()
        if self._real is not None:
            try:
                row = self._real.upsert_import(
                    student_id=sid,
                    name=display,
                    class_name=cleaned_class,
                    source=STUDENT_SOURCE_IMPORT,
                )
            except Exception:  # noqa: BLE001 - 补档失败不该中断导入后的刷新
                logger.warning("导入补档失败（%s）", sid, exc_info=True)
                row = None
            if row is not None:
                record = normalise_student_record(row)
                self._memory[sid] = record
                return record
        existing = self._memory.get(sid)
        if existing is not None:
            if display:
                existing.name = display
            if cleaned_class:
                existing.class_name = cleaned_class
            return existing
        record = StudentRecord(
            student_id=sid,
            name=display,
            class_name=cleaned_class,
            archived=False,
            source=STUDENT_SOURCE_IMPORT,
        )
        self._memory[sid] = record
        return record


def _open_student_store(cls: Any, store: Any, username: str) -> Any:
    """按几种常见构造形态打开主档（契约只写了"唯一读写口"，没写构造参数）。"""
    variants = (
        ((), {"store": store, "username": username}),
        ((), {"store": store}),
        ((store, username), {}),
        ((store,), {}),
        ((), {"username": username}),
        ((), {}),
    )
    last: Exception | None = None
    for args, kwargs in variants:
        try:
            return cls(*args, **kwargs)
        except TypeError as exc:
            last = exc
            continue
    raise TypeError(f"无法构造 {cls!r}：{last}")


def open_student_book(store: Any = None, username: str = "") -> StudentBook:
    """建一个 :class:`StudentBook`（构造本身**不抛**，降级在内部处理）。"""
    return StudentBook(store, username)


# ---------------------------------------------------------------------------
# 学号迁移（grep：core.database.migrate_student_id）
# ---------------------------------------------------------------------------

def migrate_student_id_for(old_id: str, new_id: str, *, store: Any = None,
                           username: str = "") -> Any | None:
    """调 ``core.database.migrate_student_id(旧, 新)``；契约不在时返回 ``None``。

    ★ 为什么返回 ``None`` 而不是抛：改学号是**用户点了确认**的动作，迁移助手没落地
      时我们仍然要把主档改掉（并且明确告诉用户"历史数据还挂在旧学号下"），
      不能让整个编辑流程失败。真正的失败在调用方用 "返回 None" 判定。
    """
    try:
        from core import database
    except Exception:  # noqa: BLE001
        return None
    func = getattr(database, "migrate_student_id", None)
    if not callable(func):
        warn_once(
            "migrate_student_id_missing",
            "学号迁移助手还没落地（core.database.migrate_student_id 缺失），"
            "本次只修改主档信息，历史数据仍挂在旧学号下。",
        )
        return None
    kwargs: dict[str, Any] = {}
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        params = {}
    if "store" in params:
        kwargs["store"] = store
    if "username" in params:
        kwargs["username"] = username
    return func(old_id, new_id, **kwargs)


def migration_report_text(report: Any) -> str:
    """把迁移结果翻成人话（``report.as_text()`` 优先）。"""
    if report is None:
        return "学号已修改（历史数据迁移助手未接入，数据仍挂在旧学号下）。"
    as_text = getattr(report, "as_text", None)
    if callable(as_text):
        try:
            text = str(as_text() or "").strip()
        except Exception:  # noqa: BLE001
            logger.warning("迁移报告 as_text() 失败", exc_info=True)
            text = ""
        if text:
            return text
    return str(report).strip() or "学号已修改。"


# ---------------------------------------------------------------------------
# 审计（AGENTS §25.5：只记时间 / 操作 / 学生ID / 学期 / 课程名 / 说明，**绝不写分数**）
# ---------------------------------------------------------------------------
STUDENT_AUDIT_OPS = (
    "student_add",
    "student_edit",
    "student_archive",
    "student_restore",
    "student_delete",
    "student_migrate",
)


def record_student_audit(
    store: Any,
    username: str,
    operation: str,
    *,
    student_id: str = "",
    detail: str = "",
) -> None:
    """写一条学生主档审计。

    ★ ``detail`` 是**人类可读的变更说明**（"新建学生"、"学号 2023001 → 2023042"、
      "归档隐藏"），**绝不写分数、绝不写聊天内容**。审计失败只打日志，不拦业务
      —— 审计日志是旁证，不是数据库事务。
    """
    if not username:
        return
    try:
        from core.database import GradeAuditLog
    except Exception:  # noqa: BLE001
        logger.warning("审计模块不可用，跳过 %s", operation)
        return
    op = operation if operation in STUDENT_AUDIT_OPS else operation
    try:
        log = GradeAuditLog(store=store, username=username)
    except Exception:  # noqa: BLE001
        logger.warning("初始化审计日志失败（%s）", operation, exc_info=True)
        return
    try:
        log.record(op, student_id=student_id, detail=detail)
    except TypeError:
        # 老签名：没有 detail 字段 —— 把说明塞进 course_name（仍然不写分数）
        try:
            log.record(op, student_id=student_id, course_name=detail[:60])
        except Exception:  # noqa: BLE001
            logger.warning("写审计失败（%s）", operation, exc_info=True)
    except Exception:  # noqa: BLE001
        logger.warning("写审计失败（%s）", operation, exc_info=True)


# ---------------------------------------------------------------------------
# 学期侧写（v1.9.2 新数据）
# ---------------------------------------------------------------------------
#: 学期侧写的落盘目录（相对用户密文目录）。★ 只收 ``.json.enc``：明文一律不许落盘。
SEMESTER_SIDE_DIR = "semester_side"

#: 学期侧写载荷的结构版本
SEMESTER_SIDE_SCHEMA = 1

#: 归档文件（与 ``core.database.ARCHIVE_PAYLOAD_FILE`` 同口径）
ARCHIVE_FILENAME = "archive.json.enc"


def semester_side_filename(student_id: str, semester: str) -> str:
    """``semester_side/<学号>/<学期>.json.enc``（两段都过 ``safe_student_file``）。"""
    sid = safe_student_file(student_id or "unknown")
    term = safe_student_file(semester or "unknown")
    return f"{SEMESTER_SIDE_DIR}/{sid}/{term}.json.enc"


def _semester_side_store(store: Any, username: str) -> Any:
    """建一个 ``ProfileStore``（失败返回 ``None``，调用方自己降级）。"""
    if store is None or not username:
        return None
    try:
        return ProfileStore(store=store, username=username)
    except Exception:  # noqa: BLE001
        logger.warning("初始化 ProfileStore 失败（学期侧写）", exc_info=True)
        return None


def read_semester_side(store: Any, username: str, student_id: str,
                       semester: str) -> str:
    """读某学期的侧写正文（``ProfileStore`` 优先，兜底学期文件）。"""
    sid = (student_id or "").strip()
    term = (semester or "").strip()
    if not sid or not term:
        return ""
    profile = _semester_side_store(store, username)
    if profile is not None:
        getter = getattr(profile, "semester_side_text", None)
        if callable(getter):
            try:
                text = getter(sid, term) or ""
                if text.strip():
                    return text
            except Exception:  # noqa: BLE001
                logger.warning("读学期侧写失败（ProfileStore）", exc_info=True)
    payload = load_user_json(store, username, semester_side_filename(sid, term), None)
    if isinstance(payload, dict):
        text = payload.get("text") or ""
        if isinstance(text, str):
            return text
    return ""


def save_semester_side(store: Any, username: str, student_id: str,
                       semester: str, text: str) -> bool:
    """存某学期的侧写正文（两处都写：``ProfileStore`` + 学期文件）。

    ★ 为什么两处都写：``ProfileStore`` 是主档那边的真源（迁移学号时它跟着动），
      学期文件是**兜底**（老版本库 / 主档还没接上时也能读到）。两处写同一份正文，
      读的时候 ``ProfileStore`` 优先，不会出现两个说法。
    """
    sid = (student_id or "").strip()
    term = (semester or "").strip()
    body = text or ""
    if not sid or not term:
        return False
    ok = False
    profile = _semester_side_store(store, username)
    if profile is not None:
        save = getattr(profile, "save_semester_side_text", None)
        if callable(save):
            try:
                save(sid, term, body)
                ok = True
            except Exception:  # noqa: BLE001
                logger.warning("写学期侧写失败（ProfileStore）", exc_info=True)
    try:
        save_user_json(
            store,
            username,
            semester_side_filename(sid, term),
            {
                "schema": SEMESTER_SIDE_SCHEMA,
                "student_id": sid,
                "semester": term,
                "text": body,
            },
        )
        ok = True
    except Exception:  # noqa: BLE001
        logger.warning("写学期侧写失败（学期文件）", exc_info=True)
    return ok


def delete_semester_side(store: Any, username: str, student_id: str,
                         semester: str) -> bool:
    """删某学期的侧写（删学期记录时用）。"""
    sid = (student_id or "").strip()
    term = (semester or "").strip()
    if not sid or not term:
        return False
    removed = False
    profile = _semester_side_store(store, username)
    if profile is not None:
        clearer = getattr(profile, "clear_semester_side_text", None)
        if callable(clearer):
            try:
                removed = bool(clearer(sid, term)) or removed
            except Exception:  # noqa: BLE001
                logger.warning("删学期侧写失败（ProfileStore）", exc_info=True)
        else:
            save = getattr(profile, "save_semester_side_text", None)
            if callable(save):
                try:
                    save(sid, term, "")
                    removed = True
                except Exception:  # noqa: BLE001
                    logger.warning("清空学期侧写失败（ProfileStore）", exc_info=True)
    try:
        if delete_user_json(store, username, semester_side_filename(sid, term)):
            removed = True
    except Exception:  # noqa: BLE001
        logger.warning("删学期侧写失败（学期文件）", exc_info=True)
    return removed


def delete_all_semester_sides(store: Any, username: str, student_id: str) -> int:
    """删掉某学生的**全部**学期侧写（彻底删除学生时用）。返回删掉的文件数。"""
    sid = (student_id or "").strip()
    if not sid or not username:
        return 0
    removed = 0
    profile = _semester_side_store(store, username)
    if profile is not None:
        reader = getattr(profile, "semester_sides", None)
        if callable(reader):
            try:
                sides = reader(sid) or {}
            except Exception:  # noqa: BLE001
                sides = {}
            for term in list(sides):
                if delete_semester_side(store, username, sid, term):
                    removed += 1
    folder = f"{SEMESTER_SIDE_DIR}/{safe_student_file(sid)}"
    try:
        from core.user_store import list_user_json

        for name in list_user_json(store, username):
            if name.startswith(folder + "/") and name.endswith(".json.enc"):
                if delete_user_json(store, username, name):
                    removed += 1
    except Exception:  # noqa: BLE001
        logger.warning("清理学期侧写目录失败", exc_info=True)
    return removed


# ---------------------------------------------------------------------------
# 归档 / 成绩行（学期侧写页与时间轴共用）
# ---------------------------------------------------------------------------

def load_archive(store: Any = None, username: str = "") -> Any | None:
    """读回 ``archive.json.enc``（读不到返回 ``None``，不抛）。"""
    if store is None or not username:
        return None
    payload = load_user_json(store, username, ARCHIVE_FILENAME, None)
    if not isinstance(payload, dict):
        return None
    try:
        from core.archival import Archive

        return Archive.from_dict(payload)
    except Exception:  # noqa: BLE001
        logger.warning("归档读不回来（archive.json.enc）", exc_info=True)
        return None


def student_archive_of_key(archive: Any, key: str) -> Any | None:
    """从整份归档里取某个学生的档案（``StudentArchive``）。"""
    if archive is None or not key:
        return None
    getter = getattr(archive, "get", None)
    if callable(getter):
        try:
            found = getter(key)
        except Exception:  # noqa: BLE001
            logger.warning("取学生档案失败（%s）", key, exc_info=True)
            return None
        if found is not None:
            return found
    for record in getattr(archive, "records", []) or []:
        if getattr(record, "key", "") == key:
            return record
    return None


def build_empty_student_archive(key: str, student: str, student_id: str = "") -> Any:
    """给"只在主档里有、还没有成绩"的学生造一份空档案。

    ★ 为什么需要它：详情页的肖像区会拿 ``payload["archive"]`` 去调
      :func:`core.archival.trend` / :func:`partition`，而它们都直接摸
      ``archive.terms`` —— 传 ``None`` 会 AttributeError。空档案让它们安全返回
      "单次导入，暂无趋势"。
    """
    from core.archival import StudentArchive

    return StudentArchive(key=key or student_id, student=student)


def course_rows_from_archive(student_archive: Any) -> list[dict[str, Any]]:
    """把学生档案拆成**带类别**的成绩行（``term/course/score/credit/category``）。"""
    rows: list[dict[str, Any]] = []
    for record in _ordered_records(student_archive):
        term = getattr(record, "term", "") or ""
        for course in getattr(record, "courses", []) or []:
            rows.append(
                {
                    "term": term,
                    "course": getattr(course, "name", "") or "",
                    "score": getattr(course, "score", None),
                    "credit": getattr(course, "credit", None),
                    "category": _category_text(getattr(course, "category", "")),
                }
            )
    return rows


def _category_text(value: Any) -> str:
    """类别 → 中文串（``Category.LIMITED`` / ``"限选"`` / ``None`` 都认）。"""
    if value is None:
        return ""
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    return str(value or "")


def history_rows_for(store: Any, username: str, student_id: str,
                     key: str = "") -> list[dict[str, Any]]:
    """从成绩历史表里取这名学生的**带学期**明细（裸学号 / ``sid:`` 两种键都认）。"""
    if store is None or not username:
        return []
    try:
        db = GradeHistoryDB(store=store, username=username)
        rows = db.rows()
    except Exception:  # noqa: BLE001
        logger.warning("读取成绩历史失败", exc_info=True)
        return []
    wanted = (key or "").strip() or f"{KEY_PREFIX_SID}{student_id}"
    found: list[dict[str, Any]] = []
    for row in rows:
        sid = str(row.get("student_id", "") or "").strip()
        if sid and (same_student_id(sid, wanted) or sid == (student_id or "").strip()):
            found.append(
                {
                    "term": str(row.get("semester", "") or ""),
                    "course": str(row.get("course_name", "") or ""),
                    "score": row.get("score"),
                    "credit": row.get("credit"),
                    "category": "",
                }
            )
    return found


def student_semester_rows(
    store: Any,
    username: str,
    student_id: str,
    key: str = "",
) -> dict[str, list[dict[str, Any]]]:
    """``{学期: [成绩行]}``：**归档优先**（有类别），成绩历史兜底。

    ★ 顺序一律走 :data:`core.archival.SEMESTER_ORDER`（``semester_rank``）：
      抄第二份学期表迟早漂移，症状是"时间轴上大二下排在大一上前头"（AGENTS §20a）。
    """
    rows: list[dict[str, Any]] = []
    archive = load_archive(store, username)
    student_archive = student_archive_of_key(archive, key) if archive is not None else None
    if student_archive is not None:
        rows = course_rows_from_archive(student_archive)
    if not rows:
        rows = history_rows_for(store, username, student_id, key)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        term = (row.get("term") or "").strip()
        if not term:
            continue
        grouped.setdefault(term, []).append(row)
    ordered = {
        term: grouped[term]
        for term in sorted(grouped, key=lambda t: (semester_rank(t), t))
    }
    for term_rows in ordered.values():
        term_rows.sort(key=lambda row: (_sort_score(row.get("score")), str(row.get("course", ""))))
    return ordered


def _sort_score(value: Any) -> float:
    """排序用：空分排最后（空 ≠ 0 的同一条道理）。"""
    number = _to_float(value)
    return -number if number is not None else float("inf")


def _to_float(value: Any) -> float | None:
    """宽松转数字（``None`` / 空串 / 非数字 → ``None``，**不是 0**）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 计算封装：**只转发 core**，这里没有第二份加权算术
# ---------------------------------------------------------------------------

def arithmetic_mean(values: Iterable[Any]) -> float | None:
    """算术平均（**故意不是加权** —— 学期时间轴与学期侧写就写这个词）。

    ★ 空值（``None``）不参与分母：一门课没选 / 没考不是 0 分（红线 2）。
    """
    numbers = [number for number in (_to_float(value) for value in values) if number is not None]
    return (sum(numbers) / len(numbers)) if numbers else None


def rows_arithmetic_mean(rows: Iterable[dict[str, Any]]) -> float | None:
    """成绩行 → 有分数课程的算术平均。"""
    return arithmetic_mean(row.get("score") for row in rows)


def _course_scores(rows: Iterable[dict[str, Any]]) -> list[CourseScore]:
    """成绩行 → :class:`core.calculator.CourseScore`（口径过滤的输入形态）。"""
    courses: list[CourseScore] = []
    for row in rows or ():
        score = _to_float(row.get("score"))
        credit = _to_float(row.get("credit"))
        if score is None or credit is None or credit <= 0:
            continue
        category_text = _category_text(row.get("category"))
        category = Category.UNKNOWN
        if category_text:
            try:
                category = Category(category_text)
            except ValueError:
                category = Category.UNKNOWN
        name = str(row.get("course", "") or "").strip()
        courses.append(
            CourseScore(name=name, category=category, credit=credit, score=score)
        )
    return courses


def rows_weighted_mean(
    rows: Iterable[dict[str, Any]],
    scope: CalculationScope = CalculationScope.REQUIRED_LIMITED,
) -> float | None:
    """按口径算加权均分（**复用 ``core.calculator``**，口径② 默认）。

    没有可计入的课时返回 ``None``（**不是 0.0**）—— 界面显示「—」。
    """
    courses = _course_scores(rows)
    if not courses:
        return None
    included, _excluded, _decisions = filter_by_scope(courses, scope)
    return weighted_average(included)


def rows_gpa(rows: Iterable[dict[str, Any]], scheme: str = DEFAULT_SCHEME) -> float | None:
    """GPA（方案一；``core.gpa.gpa_of``，无可算课返回 ``None``）。"""
    courses = [
        {
            "course_name": str(row.get("course", "") or ""),
            "score": row.get("score"),
            "credit": row.get("credit"),
        }
        for row in rows or ()
        if _to_float(row.get("score")) is not None and _to_float(row.get("credit")) is not None
    ]
    if not courses:
        return None
    try:
        return gpa_of(courses, scheme=scheme)
    except TypeError:
        return gpa_of(courses, scheme)
    except Exception:  # noqa: BLE001 - GPA 是派生值，算不出来不该拦住整页
        logger.warning("算 GPA 失败", exc_info=True)
        return None


def score_is_failed(value: Any) -> bool:
    """挂科判定：有分数且 < 60（**空不是挂科**，缺考 0 分算挂科）。"""
    score = _to_float(value)
    return score is not None and score < 60.0


def trend_arrow_text(delta: float | None) -> str:
    """趋势箭头：差 ≥0.05 才显示 ↑/↓，否则 →（任务书第 2 节）。"""
    if delta is None:
        return "→"
    if delta >= 0.05:
        return "↑"
    if delta <= -0.05:
        return "↓"
    return "→"


# ---------------------------------------------------------------------------
# 界面组件：学期时间轴节点
# ---------------------------------------------------------------------------
#: 时间轴节点样式（动态属性 ``tlSelected`` 是**有意加前缀**的 —— ``QWidget.maximized``
#: 那种只读属性会让 setProperty 静默失败，AGENTS §4f）。
TERM_NODE_QSS = """
QFrame#TermNode {
    background: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 10px;
}
QFrame#TermNode:hover {
    background: #F8FAFC;
    border-color: #93C5FD;
}
QFrame#TermNode[tlSelected="true"] {
    background: #EFF6FF;
    border-color: #3B82F6;
}
QLabel#TermName { color: #0F172A; font-weight: 600; }
QLabel#TermAvg { color: #475569; }
QLabel#TermTrend { color: #64748B; }
QLabel#TermTrend[tlTrend="up"] { color: #16A34A; }
QLabel#TermTrend[tlTrend="down"] { color: #EA580C; }
"""


class TermNode(QFrame):
    """学期时间轴上的一个节点（点击 = 穿透到该学期的侧写页）。

    Signals:
        clicked(str): 被点中的学期名。
    """

    clicked = Signal(str)

    def __init__(
        self,
        term: str,
        average: float | None = None,
        delta: float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.term = term or ""
        self._selected = False
        self.setObjectName("TermNode")
        self.setStyleSheet(TERM_NODE_QSS)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(72)
        self.setMinimumWidth(118)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setToolTip(f"{self.term}：点一下看这学期的学业侧写")

        box = QVBoxLayout(self)
        box.setContentsMargins(12, 8, 12, 8)
        box.setSpacing(2)

        self._name = QLabel(self.term or "未标注学期", self)
        self._name.setObjectName("TermName")
        box.addWidget(self._name)

        self._avg = QLabel(self._average_text(average), self)
        self._avg.setObjectName("TermAvg")
        box.addWidget(self._avg)

        self._trend = QLabel(self._trend_text(delta), self)
        self._trend.setObjectName("TermTrend")
        arrow = trend_arrow_text(delta)
        self._trend.setProperty(
            "tlTrend", "up" if arrow == "↑" else ("down" if arrow == "↓" else "flat")
        )
        box.addWidget(self._trend)

    @staticmethod
    def _average_text(average: float | None) -> str:
        if average is None:
            return "算术平均 —"
        return f"算术平均 {average:.2f}"

    @staticmethod
    def _trend_text(delta: float | None) -> str:
        arrow = trend_arrow_text(delta)
        if delta is None:
            return f"{arrow} 暂无对比"
        return f"{arrow} {delta:+.2f}"

    def set_selected(self, selected: bool) -> None:
        """选中态（当前正在看的那一学期）。"""
        self._selected = bool(selected)
        self.setProperty("tlSelected", "true" if self._selected else "false")
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)
        self.update()

    def is_selected(self) -> bool:
        return self._selected

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
        if event is not None and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.term)
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# 界面组件：新建 / 编辑学生表单
# ---------------------------------------------------------------------------

class StudentDialog(QDialog):
    """新建 / 编辑学生的表单（学号必填且唯一，姓名必填）。

    ★ 唯一性由 :meth:`StudentBook.add` 判（连"主档里已有"一起判）；本对话框只判
      非空。判定失败时调用方弹 ``QMessageBox.critical`` 并且**一个字节都不写盘**。
    """

    FIELD_LABELS = (
        ("student_id", "学号", "必填，全库唯一"),
        ("name", "姓名", "必填"),
        ("class_name", "班级", "选填，例如 CS2301"),
        ("note", "备注", "选填"),
    )

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        record: StudentRecord | None = None,
        title: str = "新建学生",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)
        self._record = record
        self._edits: dict[str, QLineEdit] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(10)

        hint = QLabel(
            "学号是这名学生的唯一标识：同名同姓靠它区分，成绩 / 侧写 / 聊天记录都挂在它下面。",
            self,
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        hint.setMinimumWidth(320)
        root.addWidget(hint)

        for key, label, placeholder in self.FIELD_LABELS:
            row = QHBoxLayout()
            row.setSpacing(8)
            caption = QLabel(label, self)
            caption.setObjectName("Muted")
            caption.setFixedWidth(48)
            edit = QLineEdit(self)
            edit.setObjectName("FormInput")
            edit.setPlaceholderText(placeholder)
            edit.setMinimumHeight(30)
            if record is not None:
                edit.setText(str(getattr(record, key, "") or ""))
            row.addWidget(caption)
            row.addWidget(edit, 1)
            root.addLayout(row)
            self._edits[key] = edit

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self._cancel_btn = QPushButton("取消", self)
        self._cancel_btn.setObjectName("Ghost")
        self._cancel_btn.setMinimumHeight(32)
        self._cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(self._cancel_btn)
        self._save_btn = QPushButton("保存", self)
        self._save_btn.setObjectName("Primary")
        self._save_btn.setMinimumHeight(32)
        self._save_btn.clicked.connect(self.accept)
        buttons.addWidget(self._save_btn)
        root.addLayout(buttons)

        self._edits["student_id"].setFocus()

    # ------------------------------------------------------------------ API
    def values(self) -> dict[str, str]:
        """表单当前的值（键与 :data:`STUDENT_RECORD_FIELDS` 同名）。"""
        return {key: edit.text().strip() for key, edit in self._edits.items()}

    def student_id(self) -> str:
        return self.values().get("student_id", "")

    def accept(self) -> None:  # noqa: D102 - QDialog 约定
        values = self.values()
        if not values.get("student_id"):
            QMessageBox.warning(self, "信息不完整", "学号是必填的：它决定成绩/侧写挂在谁名下。")
            self._edits["student_id"].setFocus()
            return
        if not values.get("name"):
            QMessageBox.warning(self, "信息不完整", "姓名是必填的。")
            self._edits["name"].setFocus()
            return
        super().accept()

