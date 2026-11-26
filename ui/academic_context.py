"""在本地模型的上下文预算内提供全学期成绩。"""

from dataclasses import dataclass
from itertools import zip_longest
from typing import Callable

from core.ai.inference import InferenceUnavailable


@dataclass(frozen=True)
class AcademicContext:
    summary: str
    courses: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]

    @property
    def course_count(self) -> int:
        return sum(len(rows) for _, rows in self.courses)

    def render(self, prompt: str, question: str, token_limit: int, count: Callable[[str], int],
               *, preamble: str = "") -> str:
        def assemble(rows: list[str], *, complete: bool) -> str:
            coverage = "课程明细为全部记录。" if complete else (
                "各学期统计为完整结果，下面的课程明细按问题与上下文容量选取。"
                "仅根据实际提供的课程回答，可请用户注明学期或课程以查询其余明细。"
            )
            return f"{preamble}{self.summary}\n{coverage}\n" + "\n".join(rows) + "\n\n" + prompt

        all_rows = [detail for _, courses in self.courses for _, detail in courses]
        full = assemble(all_rows, complete=True)
        if count(full) <= token_limit:
            return full
        chosen: list[str] = []
        if count(assemble(chosen, complete=False)) > token_limit:
            prompt = "请用中文回答当前问题，依据成绩数据给出判断。\n问题：" + question
        if count(assemble(chosen, complete=False)) > token_limit:
            raise InferenceUnavailable("问题与学期总评超出模型的上下文容量。请缩短问题。")

        groups = [
            sorted(courses, key=lambda row: not (row[0] and row[0].casefold() in question.casefold()))
            for _, courses in sorted(self.courses, key=lambda item: item[0] not in question)
        ]
        for group in zip_longest(*groups):
            for row in group:
                if row is None:
                    continue
                candidate = [*chosen, row[1]]
                if count(assemble(candidate, complete=False)) <= token_limit:
                    chosen.append(row[1])
        return assemble(chosen, complete=False)
