"""本地学习助手 —— **占位骨架，没有模型，也没有网络**。

charter 6.1：``LocalStudyAssistant.evaluate_student(student_dict) -> str``，
当前返回占位文字"本地模型未加载"。

★ 为什么现在只留一个类
--------------------

charter 6.3 明令：**不写实际 AI 调用代码**、**不引入 torch / transformers**。
所以这里刻意不 import 任何东西 —— 它只定义接口形状，
让「设置 → AI 助手」那两个开关有东西可指，也让后续阶段有个明确的落点。

★ 接入时要注意什么（留给下一个实现者）
-----------------------------------

1. **数字不能由模型算**（v1.3 红线 1）。这个方法的入参必须已经是
   ``core.calculator`` 算好的结果，模型只负责把数字翻译成人话。
2. **它不联网**。真要跑本地模型，模型文件也必须在用户机器上，
   不许在调用时下载。
3. **返回的永远是 str**，且必须是**可以直接显示给辅导员看的成品**，
   不是 JSON、不是 markdown 表格 —— 调用方会直接塞进 QLabel / QTextEdit。
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["PLACEHOLDER", "LocalStudyAssistant"]

#: 占位文案。**改它之前先想清楚**：界面上要能一眼看出"这一块还没接"，
#: 所以这句话必须明确说"没加载"，不能写成"正在生成…"那种骗人的话。
PLACEHOLDER = "本地模型未加载"

#: 一句话说明这一层打算做什么（设置页的说明文字用它）
DESCRIPTION = "本地助手不联网，将来可离线生成辅导提纲。"


class LocalStudyAssistant:
    """本地学习助手（占位）。

    调用方现在就会拿到 :data:`PLACEHOLDER`，永远不会抛异常 ——
    界面上一句"本地模型未加载"比一个 traceback 有用得多。
    """

    #: 给界面判断"接上了没有"用。现在恒为 False。
    available = False

    def __init__(self, **options: Any) -> None:
        # 保留 options：将来要传模型路径 / 线程数 / 温度这类参数时，
        # 调用方不用改签名。
        self.options: dict[str, Any] = dict(options)

    # ------------------------------------------------------------------
    def evaluate_student(self, student_dict: Mapping[str, Any] | None) -> str:
        """给一个学生出一段辅导建议。

        Args:
            student_dict: 侧写页那一份原料（均分、趋势、定型/可救分区…）。
                现在**用不到**，但签名先定下来 —— charter 6.1 就是这么写的。

        Returns:
            永远是 str。当前恒为 :data:`PLACEHOLDER`。
        """
        return PLACEHOLDER

    def is_ready(self) -> bool:
        """模型加载好了没有。现在恒 False。"""
        return False
