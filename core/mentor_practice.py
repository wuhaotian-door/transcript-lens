"""认人练习的出题和错题规则。"""

import random
from datetime import datetime
from uuid import uuid4

ROUND_LENGTH = 20
MODE_NAMES = "name"
MODE_PHOTOS = "photo"
MODE_REVIEW = "review"
MODE_LABELS = {
    MODE_NAMES: "看照片选名字",
    MODE_PHOTOS: "看名字选照片",
    MODE_REVIEW: "错题练习",
}


def new_round(scope: str, kind: str, total: int) -> dict:
    return {
        "id": uuid4().hex,
        "scope": scope,
        "kind": kind,
        "total": total,
        "started_at": datetime.now().isoformat(sep=" ", timespec="microseconds"),
        "answers": [],
    }


def regular_targets(student_ids: list[str], mode: str) -> list[tuple[str, str]]:
    """池内先不重复出题。用完后重新洗牌，避免两批交界连续出现同一人。"""
    if len(student_ids) < 2:
        return []
    chosen = []
    while len(chosen) < ROUND_LENGTH:
        order = random.sample(student_ids, len(student_ids))
        if chosen and order[0] == chosen[-1]:
            order[0], order[1] = order[1], order[0]
        chosen.extend(order)
    return [(sid, mode) for sid in chosen[:ROUND_LENGTH]]


def review_targets(rounds: list[dict], ids_by_ref: dict[str, str]) -> list[tuple[str, str]]:
    """同一学生的两种题型分别累计，最近一次答对即移出错题。"""
    latest = {}
    answers = sorted((answer for round_ in rounds for answer in round_["answers"]),
                     key=lambda answer: answer["at"])
    for answer in answers:
        sid = ids_by_ref.get(answer["target"])
        if sid is not None:
            latest[sid, answer["mode"]] = answer["correct"]
    pending = [question for question, correct in latest.items() if not correct]
    return random.sample(pending, min(ROUND_LENGTH, len(pending)))
