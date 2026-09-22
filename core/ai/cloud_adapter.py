"""云端大模型适配器 —— **占位骨架。一行网络代码都没有，也不许有。**

charter 6.1：``CloudAdapter.deep_evaluate(student_dict, model_choice) -> str``，
当前返回占位 + 提示"需配置 API Key"。

★★ 这一层的红线（charter 第 3 章 + v1.3 红线 6）★★

| 规则 | 本文件怎么保证 |
|---|---|
| 用户**主动**才能调用 | 只有 :meth:`deep_evaluate` 一个出口，界面上只挂在「深度评估」按钮上 |
| 绝不静默联网 | 本文件**不 import 任何 HTTP / SDK 库**；`main.py --selfcheck` 有断言盯着 |
| 传出去必须脱敏 | 唯一出口是 :func:`redact_student`，白名单制 |
| API Key 存加密库 | 由调用方从 ``CryptoStore`` 取，本文件不读磁盘、不读环境变量 |

★ 为什么现在连 `import requests` 都不写
------------------------------------

因为写了就会**被打进包**，而 `--selfcheck` 里那组"未打包 requests / httpx /
openai / anthropic"的断言正是为了防止"Agent 偷懒乱联网"。将来真要接，
**必须同时**改那组断言并说明理由 —— 不允许悄悄加一个 import 了事。
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "NOT_CONFIGURED",
    "REDACTED_FIELDS",
    "CloudAdapter",
    "redact_student",
]

#: 没配 Key / 没启用时的占位文案（charter 6.1 要求提到"需配置 API Key"）
NOT_CONFIGURED = "云端大模型未启用：需配置 API Key"

#: 说明文字（设置页用，charter 6.2 指定的原话）
DESCRIPTION = "云端大模型仅在您主动点击「深度评估」时调用。"

#: 允许传出机器的字段白名单（v1.3 红线 6：禁止学生真名、真学号、身份证）
#:
#: ★ **白名单制**，不是黑名单：以后要加字段必须显式加到这里，
#:   而不是"忘了排除某个敏感字段"就漏出去。
REDACTED_FIELDS: tuple[str, ...] = (
    "S编号",       # S001 这种编号，不含真学号
    "加权均分",
    "总学分",
    "课程学分",
    "分数区间",
    "意愿标签",
    "挂科数",
    "缺考数",
)

#: 这些字段**永远不许**出现在 payload 里。留着是为了能写断言。
FORBIDDEN_FIELDS: tuple[str, ...] = (
    "姓名", "学号", "身份证", "student", "student_id", "name",
)


def redact_student(student_dict: Mapping[str, Any] | None) -> dict[str, Any]:
    """把一份学生数据裁成**只剩白名单字段**的脱敏 payload。

    ★ 这是这一层唯一的出口格式化函数。任何要发出去的东西都必须先过它。

    做法是 ``{k: v for k, v in ... if k in REDACTED_FIELDS}`` ——
    **白名单制**：没在名单里的东西一律不出去，包括以后新加的字段。
    这比"排掉姓名/学号"的黑名单安全得多：黑名单漏一个就是泄漏，
    白名单漏一个只是少传一个字段。
    """
    if not student_dict:
        return {}
    return {
        key: value
        for key, value in dict(student_dict).items()
        if key in REDACTED_FIELDS
    }


class CloudAdapter:
    """云端大模型适配器（占位）。

    Args:
        api_key: 从加密库读出来的 Key。**空串 = 未配置**。
        model_choice: 用户在设置页选的那个模型名。
    """

    #: 给界面判断"接上了没有"用。现在恒为 False。
    available = False

    def __init__(self, api_key: str = "", model_choice: str = "", **options: Any) -> None:
        # ★ 只存"配没配"，**不存 Key 本身到任何能被打印的地方**。
        #   charter 铁律 2：密码/授权码/找回码/库密钥绝不 print、不回显、不进日志。
        self._configured = bool(api_key)
        self.model_choice = model_choice or ""
        self.options: dict[str, Any] = dict(options)

    # ------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return self._configured

    def deep_evaluate(self, student_dict: Mapping[str, Any] | None,
                      model_choice: str | None = None) -> str:
        """深度评估（占位）。

        真接上之后的流程应当是：

        1. ``payload = redact_student(student_dict)`` ← **必须先过脱敏**
        2. 发请求（只在用户点了「深度评估」时）
        3. 把结果整理成一段可以直接显示的文字

        Returns:
            永远是 str。当前：配了 Key 就提示"尚未接通"，没配就提示要配 Key。
        """
        if not self._configured:
            return NOT_CONFIGURED
        chosen = model_choice or self.model_choice or "（未选择）"
        return f"云端大模型接口尚未接通（已选择：{chosen}）。本版本只预留接口，不会发起任何请求。"

    def is_ready(self) -> bool:
        """能不能真的调用。现在恒 False —— 骨架阶段就是不能。"""
        return False
