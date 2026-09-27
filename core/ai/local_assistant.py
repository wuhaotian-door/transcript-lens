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

from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "DESCRIPTION",
    "MODEL_LOADED_PLACEHOLDER",
    "PLACEHOLDER",
    "LocalStudyAssistant",
    "find_local_model",
]

#: 占位文案。**改它之前先想清楚**：界面上要能一眼看出"这一块还没接"，
#: 所以这句话必须明确说"没加载"，不能写成"正在生成…"那种骗人的话。
PLACEHOLDER = "本地模型未加载"

#: 模型文件已经躺在磁盘上、但**没有推理引擎**时的文案（charter v1.8 第八节：
#: "``evaluate_student()`` 返回『模型已加载但未配置推理引擎』"）。
#:
#: ★ 为什么不直接说"能用"：v1.8 只做到"把 GGUF 下下来 + 认出来"。
#:   真跑推理要引 llama-cpp / torch 那类重依赖，charter 明确说
#:   "占位，不实现推理"。所以这一句必须**同时**说明"模型找到了"和
#:   "还算不了" —— 少说前半句用户以为没下载成功，少说后半句用户以为能用了。
MODEL_LOADED_PLACEHOLDER = "模型已加载但未配置推理引擎"

#: 一句话说明这一层打算做什么（设置页的说明文字用它）
DESCRIPTION = "本地助手不联网，将来可离线生成辅导提纲。"


def find_local_model(dest_dir: str | Path | None = None) -> Path | None:
    """找出已经下载好的本地模型文件（没有就返回 ``None``）。

    ★ 复用 ``model_downloader.installed_models`` 那一套判据
      （"目录里有非空的 .gguf"）—— 两边各写一份的话，
      设置页显示"已下载"而这里说"没加载"这种自相矛盾迟早出现。
    """
    from core.ai.model_downloader import installed_models

    for info in installed_models(dest_dir):
        if info.path is not None:
            return info.path
    return None


class LocalStudyAssistant:
    """本地学习助手（骨架：会认模型，不会推理）。

    ==================  ==================================================
    情形                  :meth:`evaluate_student` 返回
    ==================  ==================================================
    没下载模型            :data:`PLACEHOLDER`（"本地模型未加载"）
    下载了模型但没引擎     :data:`MODEL_LOADED_PLACEHOLDER`
    ==================  ==================================================

    永远不会抛异常 —— 界面上一句人话比一个 traceback 有用得多。
    """

    #: 给界面判断"接上了没有"用。现在恒为 False（没有推理引擎）。
    available = False

    def __init__(self, model_path: str | Path | None = None, **options: Any) -> None:
        # 保留 options：将来要传模型路径 / 线程数 / 温度这类参数时，
        # 调用方不用改签名。
        self.options: dict[str, Any] = dict(options)
        #: 显式给的模型路径优先；没给就去 models/ 里找一个。
        self.model_path: Path | None = (
            Path(model_path) if model_path is not None
            else find_local_model(self.options.get("models_dir"))
        )

    # ------------------------------------------------------------------
    @property
    def model_loaded(self) -> bool:
        """磁盘上有没有一个可用的模型文件。"""
        return self.model_path is not None and self.model_path.exists()

    @property
    def model_name(self) -> str:
        """模型名（``"1.5B"``）；没加载就是空串。给设置页显示用。"""
        if not self.model_path:
            return ""
        return self.model_path.parent.name or self.model_path.name

    def evaluate_student(self, student_dict: Mapping[str, Any] | None) -> str:
        """给一个学生出一段辅导建议。

        Args:
            student_dict: 侧写页那一份原料（均分、趋势、定型/可救分区…）。
                现在**用不到**，但签名先定下来 —— charter 6.1 就是这么写的。

        Returns:
            永远是 str。没模型 → :data:`PLACEHOLDER`；
            有模型 → :data:`MODEL_LOADED_PLACEHOLDER`（v1.8 不实现推理）。
        """
        if not self.model_loaded:
            return PLACEHOLDER
        return MODEL_LOADED_PLACEHOLDER

    def is_ready(self) -> bool:
        """**能不能真的算**。现在恒 False —— 模型在也不算，

        ★ 注意和 :attr:`model_loaded` 的区别：那个回答"文件在不在"，
          这个回答"能不能出辅导建议"。v1.8 只做到前者。
        """
        return False

    def describe(self) -> str:
        """给设置页用的一句话现状（纯文本，**不写 Markdown** —— 铁律 9）。"""
        if not self.model_loaded:
            return f"{DESCRIPTION}（当前：{PLACEHOLDER}）"
        return (
            f"已找到本地模型「{self.model_name}」，"
            f"但 {MODEL_LOADED_PLACEHOLDER} —— 本版本只预留接口。"
        )
