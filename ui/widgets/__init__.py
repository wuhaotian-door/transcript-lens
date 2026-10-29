"""可复用的小组件（v1.9.2 起）。

目前只有一个 :class:`ui.widgets.backend_setup.BackendSetupCard`：

* 推理后端（``llama-cpp-python``）没装时，它是**唯一**的安装入口 —— 模型中心与
  档案页「AI 辅导」共用同一张卡片（AGENTS §36）。
* 组件本身**绝不静默安装**：只有用户点了「一键安装」才会真的起 pip 子进程。

★ 本包里的控件一律**不许**给整页挂 ``QGraphicsOpacityEffect`` /
``QGraphicsDropShadowEffect``（AGENTS §4a / §4b / §17）。
"""

from __future__ import annotations

from ui.widgets.backend_setup import BackendSetupCard

__all__ = ["BackendSetupCard"]
