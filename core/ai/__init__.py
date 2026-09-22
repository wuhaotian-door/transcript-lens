"""AI 接口预留层（**骨架，不实现任何真实调用**）。

Spec v1.6 charter 第 6 章：这一层现在只留接口和占位文字，等后续阶段再填实现。

    core/ai/
    ├─ __init__.py
    ├─ local_assistant.py    LocalStudyAssistant.evaluate_student()  → 占位
    └─ cloud_adapter.py      CloudAdapter.deep_evaluate()            → 占位

★★ 三条硬约束（改这一层之前先读）★★

1. **这里一行网络代码都没有，也不许有。**
   ``CloudAdapter`` 现在**不 import 任何 HTTP 库**（charter 第 3 章明令
   `requests` / `httpx` 不得用于非 SMTP / 非用户主动 AI 场景；
   `main.py --selfcheck` 里有一组断言专门盯这件事）。
   将来接云端时，调用必须**由用户点「深度评估」才发生**，绝不静默。

2. **传出去的东西必须脱敏**（v1.3 红线 6）。
   :func:`core.ai.cloud_adapter.redact_student` 是唯一的出口格式化函数，
   它只保留「S 编号 + 均分 + 学分 + 分数区间 + 意愿标签」，
   **姓名、学号、身份证一律剥掉**。要加字段只能加到这个白名单里。

3. **本地助手不联网。**
   :class:`LocalStudyAssistant` 将来若接本地模型，也只在本机跑；
   charter 6.3 明令现在**不许**引入 torch / transformers 依赖。
"""

from __future__ import annotations

__all__ = ["CloudAdapter", "LocalStudyAssistant"]


def __getattr__(name: str):
    """懒加载 —— import ``core.ai`` 本身不拖任何重东西。

    ★ 用 ``__getattr__`` 而不是在模块顶层 import：
      设置页只是要显示两个开关，没必要把两个适配器都拉起来。
      将来 ``cloud_adapter`` 真接上 SDK 之后，这一层就是"不开云端的用户
      根本不会 import 到联网库"的保证。
    """
    if name == "LocalStudyAssistant":
        from core.ai.local_assistant import LocalStudyAssistant

        return LocalStudyAssistant
    if name == "CloudAdapter":
        from core.ai.cloud_adapter import CloudAdapter

        return CloudAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
