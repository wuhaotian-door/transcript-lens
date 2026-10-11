"""「AI 谈话助手」对话框（v1.9 第二阶段 C3）。

一句话：**左边看谈话、右边打字、上面挑模型**，全部离线跑在本机。

★ 五条设计约束（改这个文件之前先读）
====================================

1. **明文成绩绝不落盘**。谈话记录按日期分组，写进**应用级加密库**：
   ``%LOCALAPPDATA%\\SGMS\\u_<uid>\\chat_history.json.enc``
   （``core.user_store.save_user_json``）。任务书原文写的是
   ``cache/chat_history.json``，那与"磁盘上只有密文"的红线冲突 ——
   **以红线为准**，所以落盘位置改到加密库里，磁盘上只有 ``.enc``。
2. **不许在主线程里同步阻塞**。加载模型要几秒、生成要几十秒，
   全部丢进 :class:`_ChatWorker`（``QThread``）；主线程只在信号回调里碰控件
   （AGENTS.md 第 5b / 21a 节：请求必须走后台线程，否则窗口"无响应"）。
3. **引擎不可用时界面照开**：顶部一行中文说明、输入区禁用，
   **不弹一串模态框**（弹窗会把自动化探针的看门狗跑满）。
4. **界面文案不许有 Markdown 记号、不许有版本号字面量**
   （AGENTS.md 第 24 / 23a 节）：这些都是纯文本 QLabel，``**`` 会原样显示。
5. :func:`open_chat` 是**冻结接口** —— D 任务（侧写页）只按这个签名调用，
   改签名前先跟它商量：

       open_chat(parent, *, system_prompt=None, initial_prompt=None,
                 prefer_model=None) -> ChatDialog

   它内部完成：建对话 → 把 ``initial_prompt`` 填进上下文并**自动开始生成** →
   ``show()`` 出来（**非模态**，侧写页不用等它）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.ai.inference import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    InferenceUnavailable,
    LocalModel,
    LocalModelEngine,
    engine_available,
    friendly_error,
    list_local_models,
)

__all__ = [
    "CHAT_HISTORY_FILE",
    "ChatDialog",
    "SYSTEM_PROMPT",
    "open_chat",
]

#: 系统提示词 —— **逐字**照任务书给的，一个字都不许改。
SYSTEM_PROMPT = (
    "你是一名高校辅导员助理，擅长学生谈话提纲生成、评语润色、侧写分析。"
    "请根据对话上下文提供专业建议。"
)

#: 谈话记录的文件名。放在用户自己的加密目录 ``u_<uid>/`` 下，
#: 后缀必须是 ``.enc`` —— ``save_encrypted`` 会当场拒绝写成 .json
#: （AGENTS.md 第 32 节踩过这一条）。
CHAT_HISTORY_FILE = "chat_history.json.enc"

#: 载荷结构版本。将来改结构时靠它迁移。
HISTORY_SCHEMA = 1

#: 每天最多留多少轮。聊天记录会越积越多，落盘文件必须有个上限。
MAX_TURNS_PER_DAY = 200

#: 拼提示词时最多带最近多少轮（多了会撑爆 n_ctx=2048 的上下文窗口）。
MAX_CONTEXT_TURNS = 12

#: 提示词的字符上限（双保险：一轮很长时按字符再裁一次）。
MAX_PROMPT_CHARS = 4000

#: 生成时的停止串：模型一旦开始"扮演辅导员"就停。
STOP_WORDS = ("\n辅导员：", "辅导员：")

#: 谈话记录里的两种角色。存盘用英文键（结构稳定），显示才翻成中文。
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

_SPEAKER = {ROLE_USER: "辅导员", ROLE_ASSISTANT: "你"}

#: 线程还在跑、但对话框已经关掉时，**必须**留个引用给它 ——
#: 一个还在运行的 ``QThread`` 被 Python 回收会让进程直接消失
#: （AGENTS.md 第 4k 节那类 use-after-free 的同族问题）。
_LIVE_WORKERS: set[QThread] = set()


def _now() -> datetime:
    return datetime.now()


def _day_key(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d")


def _clock(moment: datetime) -> str:
    return moment.strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# 历史：读、写、归一化（**只走应用级加密库**）
# ---------------------------------------------------------------------------


def _normalize_history(payload: Any) -> dict[str, list[dict[str, str]]]:
    """把磁盘上读回来的载荷收敛成 ``{日期: [轮次, ...]}``。

    坏数据一律**丢掉那一部分**而不是抛异常：谈话记录是便利数据，
    坏掉不该让对话框打不开。
    """
    days: dict[str, list[dict[str, str]]] = {}
    if not isinstance(payload, Mapping):
        return days
    raw_days = payload.get("days")
    if not isinstance(raw_days, Mapping):
        return days
    for day, turns in raw_days.items():
        if not isinstance(turns, list):
            continue
        clean: list[dict[str, str]] = []
        for turn in turns:
            if not isinstance(turn, Mapping):
                continue
            text = str(turn.get("text") or "").strip()
            if not text:
                continue
            role = str(turn.get("role") or "")
            clean.append(
                {
                    "role": ROLE_ASSISTANT if role == ROLE_ASSISTANT else ROLE_USER,
                    "text": text,
                    "time": str(turn.get("time") or ""),
                    "model": str(turn.get("model") or ""),
                }
            )
        if clean:
            days[str(day)] = clean[-MAX_TURNS_PER_DAY:]
    return days


def _coerce_prompt(value: Any) -> str:
    """把 ``initial_prompt`` 变成一段可以直接进提示词的纯文本。

    ★ 允许调用方（侧写页）传一份**学生字典**：那就**必须**先过
      :func:`core.ai.cloud_adapter.redact_student`（白名单制）——
      侧写页那份原料里带着姓名和学号，明文姓名进提示词是铁律
      （AGENTS.md 第 9 节）。拿不到那个函数时**一个字都不输出**，
      宁可少说，也不能漏。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        try:
            from core.ai.cloud_adapter import redact_student
        except Exception:  # noqa: BLE001 - 脱敏函数不在就整段放弃
            return ""
        clean = redact_student(value)
        return "、".join(f"{key}：{item}" for key, item in clean.items())
    if isinstance(value, (list, tuple)):
        parts = [_coerce_prompt(item) for item in value]
        return "；".join(part for part in parts if part)
    return str(value).strip()


# ---------------------------------------------------------------------------
# 后台生成
# ---------------------------------------------------------------------------


class _ChatWorker(QThread):
    """在子线程里"加载模型 + 逐块取字"。

    ★ 为什么非要线程：``load()`` 要几秒（1~3 GB 的模型），``stream()`` 一次
      几十秒。放在主线程里，窗口不重绘、Windows 会弹"无响应"。
    ★ 信号是跨线程的（Qt 自动排队），所以回调里碰控件是安全的。
    """

    #: 新的一块文本（可能是一两个 token）
    chunk = Signal(str)
    #: 失败了，附一句**中文人话**（已经翻过、抹掉包管理指令）
    failed = Signal(str)

    def __init__(
        self,
        engine: Any,
        prompt: str,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        stop: Sequence[str] | None = None,
    ) -> None:
        super().__init__()
        self._engine = engine
        self._prompt = prompt
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._stop = tuple(stop or ())
        self._cancelled = False
        #: 已经吐出来的整段回答（**线程侧**自己攒，见 :meth:`ChatDialog._record_partial`）
        self._answer = ""

    def cancel(self) -> None:
        """请求停止（主线程调用）。流循环每块之间查一次。"""
        self._cancelled = True
        self.requestInterruption()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def answer(self) -> str:
        """线程侧攒下的回答原文（半途停下时也已经有一段）。"""
        return self._answer

    def run(self) -> None:  # pragma: no cover - 线程体，靠界面测试覆盖
        engine = self._engine
        try:
            engine.load()
        except InferenceUnavailable as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(friendly_error(exc))
            return
        try:
            for piece in engine.stream(
                self._prompt,
                self._max_tokens,
                self._temperature,
                stop=self._stop or None,
            ):
                if self._cancelled or self.isInterruptionRequested():
                    break
                self._answer += piece
                self.chunk.emit(piece)
        except InferenceUnavailable as exc:
            if not self._cancelled:
                self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            if not self._cancelled:
                self.failed.emit(friendly_error(exc))


# ---------------------------------------------------------------------------
# 对话框
# ---------------------------------------------------------------------------


class ChatDialog(QDialog):
    """AI 谈话助手：左边看记录、右边打字、上面挑模型。

    Args:
        parent: 父窗口。**非模态**打开（``show()``），所以家长照常能用。
        store / username: 谈话记录落盘用。不给就从 ``parent`` 上取
            （主窗有 ``_store`` / ``_username``）；再取不到就只留内存，
            聊天照常，只是关掉窗口就不留记录。
        engine: 注入一个引擎（测试用）。给了它就忽略下拉框里的模型。
        models: 注入模型清单（测试用）。不给就扫磁盘。
        system_prompt: 覆盖系统提示词。不给就用 :data:`SYSTEM_PROMPT`。
        prefer_model: 想优先选中的模型名（命不中就退回第一个）。

    ★ 引擎不可用 / 没有可用的模型时，这个窗口**照样打开**：
      顶部一行中文说明、输入区禁用，不弹任何模态框。
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        store: Any = None,
        username: str = "",
        engine: Any = None,
        models: Sequence[LocalModel] | None = None,
        system_prompt: str | None = None,
        prefer_model: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI 谈话助手")
        self.resize(980, 640)
        self.setMinimumSize(760, 480)

        self._store = store if store is not None else getattr(parent, "_store", None)
        self._username = username or str(getattr(parent, "_username", "") or "")
        self._injected_engine = engine
        self._system_prompt = (system_prompt or SYSTEM_PROMPT).strip()

        self._models: list[LocalModel] = list(models) if models is not None else []
        self._choices: list[LocalModel] = []
        self._engine: Any = None
        self._worker: _ChatWorker | None = None
        self._days: dict[str, list[dict[str, str]]] = {}
        self._streaming = False
        # 上一次生成失败的原因。_on_worker_finished 会在收尾时把它重新贴上状态行 ——
        # 不然 _refresh_availability() 的一句"可以开始谈话"会把错误说明当场抹掉，
        # 用户只看到输入区又亮了，却不知道刚才为什么没答上来。
        self._last_error = ""
        # 这一轮回答是否已经在记录框里"落笔"了（流式显示时只在第一块写一次说话人）
        self._answer_open = False

        self._build()
        if models is None:
            self._scan_models()
        else:
            self._apply_models()
        self._load_history()
        self._render_history()
        self._refresh_availability(prefer_model)

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(10)

        head = QLabel("AI 谈话助手")
        head.setObjectName("H3")
        root.addWidget(head)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        # ★ 需要换行的 label 必须给宽度约束，否则会把窗口撑宽（AGENTS.md 4c）
        self.status_label.setMinimumWidth(420)
        root.addWidget(self.status_label)

        picker = QHBoxLayout()
        picker.setSpacing(8)
        picker.addWidget(QLabel("模型："))
        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(320)
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        picker.addWidget(self.model_combo, 1)
        root.addLayout(picker)

        body = QHBoxLayout()
        body.setSpacing(14)

        self.history_view = QTextEdit()
        self.history_view.setObjectName("Outline")
        self.history_view.setReadOnly(True)
        self.history_view.setMinimumWidth(420)
        self.history_view.setPlaceholderText("这里显示谈话记录。")
        body.addWidget(self.history_view, 1)

        side = QVBoxLayout()
        side.setSpacing(8)
        # ★ 输入区贴着右侧栏**底部**：最新的记录出现在左边记录框的末尾，
        # 打字的地方就在同一水平线上（聊天窗的常规排布），不用来回找。
        side.addStretch(1)
        side.addWidget(QLabel("输入"))

        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("说点什么…（回车发送）")
        self.input_edit.returnPressed.connect(self._on_send_clicked)
        side.addWidget(self.input_edit)

        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("Primary")
        self.send_button.setMinimumHeight(32)
        self.send_button.clicked.connect(self._on_send_clicked)
        side.addWidget(self.send_button)

        self.export_button = QPushButton("导出文本")
        self.export_button.setObjectName("Ghost")
        self.export_button.setMinimumHeight(30)
        self.export_button.clicked.connect(self._on_export_clicked)
        side.addWidget(self.export_button)

        note = QLabel("谈话记录加密存在本机，不会上传。")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        note.setFixedWidth(240)
        side.addWidget(note)

        holder = QWidget()
        holder.setLayout(side)
        holder.setFixedWidth(250)
        body.addWidget(holder)
        root.addLayout(body, 1)

    # ------------------------------------------------------------ 模型清单
    def _scan_models(self) -> None:
        """扫磁盘上真的装好了的模型（拿不到清单就是空表，不报错）。"""
        try:
            self._models = list_local_models()
        except Exception:  # noqa: BLE001 - 清单坏了不该让对话框打不开
            self._models = []
        self._apply_models()

    def _apply_models(self) -> None:
        """填下拉框：**只列跑得动的**（拿不到负载等级就列全部已装）。"""
        self._choices = [model for model in self._models if getattr(model, "usable", True)]

        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        if self._choices:
            for model in self._choices:
                self.model_combo.addItem(model.display, model.name)
        else:
            self.model_combo.addItem("（没有可用的本地模型）", "")
        self.model_combo.blockSignals(False)
        self.model_combo.setEnabled(bool(self._choices))

    def _select(self, prefer_model: str | None) -> None:
        """按名字挑一个模型；命不中就选第一个（任务书：退回默认）。"""
        if not self._choices:
            return
        wanted = (prefer_model or "").strip().lower()
        index = 0
        if wanted:
            for position, model in enumerate(self._choices):
                names = {model.name.lower(), (model.label or "").lower(), model.path.name.lower()}
                if wanted in names or any(wanted in name for name in names if name):
                    index = position
                    break
        self.model_combo.setCurrentIndex(index)

    def model(self) -> LocalModel | None:
        """当前选中的模型（没有就是 ``None``）。"""
        index = self.model_combo.currentIndex()
        if 0 <= index < len(self._choices):
            return self._choices[index]
        return None

    def _engine_for_current(self) -> Any:
        """当前该用哪个引擎：注入的优先，否则按选中的模型建一个常驻实例。"""
        if self._injected_engine is not None:
            return self._injected_engine
        model = self.model()
        if model is None:
            raise InferenceUnavailable(
                "还没有可用的本地模型。请先在「模型中心」里下载一个，再回来谈话。"
            )
        engine = self._engine
        if engine is not None and getattr(engine, "model_path", None) == model.path:
            return engine
        if engine is not None:
            try:
                engine.unload()
            except Exception:  # noqa: BLE001 - 换模型时释放失败不该拦住谈话
                pass
        self._engine = LocalModelEngine(model.path)
        return self._engine

    def _on_model_changed(self, _index: int) -> None:
        """换模型 = 把上一个从内存里放掉（1~3 GB，不能同时留着两个）。"""
        if self._injected_engine is not None:
            return
        engine = self._engine
        if engine is None:
            return
        current = self.model()
        if current is not None and getattr(engine, "model_path", None) == current.path:
            return
        try:
            engine.unload()
        except Exception:  # noqa: BLE001
            pass
        self._engine = None

    # ------------------------------------------------------------ 可用状态
    def _refresh_availability(self, prefer_model: str | None = None) -> None:
        """刷新状态行 + 输入区的启用状态。**一次都不弹窗。**"""
        available, engine_message = engine_available()
        message = engine_message
        if self._injected_engine is not None:
            available = True
        engine_ready = available

        if self._choices:
            self._select(prefer_model)
        elif self._models:
            message = (
                "本机已装的模型都太重了（负载等级不是轻松或勉强），"
                "建议到「模型中心」换一个更小的模型。"
            )
            available = False
        else:
            message = (
                "本机还没有下载模型。请先到「模型中心」下载一个，再回来谈话。"
            )
            available = False

        if available and self._choices:
            message = f"可以开始谈话。当前有 {len(self._choices)} 个可用的本地模型。"

        if not engine_ready:
            # ★ 引擎没装是**更根本**的原因：这时只说"去下模型""模型太重"都是句假话
            # —— 用户下完几个 GB 回来照样用不了。所以引擎的说明优先，模型层面的事
            # 跟在后面补一句（AGENTS.md 第 21b 节同族：界面上不许说假话）。
            available = False
            if self._choices:
                hint = "装好之后，本机已有的模型就能用了。"
            elif self._models:
                hint = "另外，本机已有的模型都偏重，装好之后建议换一个更小的。"
            else:
                hint = "装好之后，还需要在「模型中心」下载一个模型。"
            message = f"{engine_message}{hint}"

        self.status_label.setText(message)
        self.status_label.setObjectName("Muted" if available else "Warn")
        # ★ 动态属性只影响 QSS 选择器，改完要重新抛光一次才会重画
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

        usable = bool(self._choices) and available
        self.input_edit.setEnabled(usable)
        self.send_button.setEnabled(usable)
        self.export_button.setEnabled(bool(self._history_turns()))

    # ---------------------------------------------------------------- 历史
    def _history_turns(self) -> list[dict[str, str]]:
        """按日期顺序摊平的谈话记录。"""
        turns: list[dict[str, str]] = []
        for day in sorted(self._days):
            turns.extend(self._days[day])
        return turns

    def _load_history(self) -> None:
        """从加密库读回谈话记录（读不到就是空表，不报错）。"""
        self._days = {}
        if self._store is None or not self._username:
            return
        try:
            from core.user_store import load_user_json

            payload = load_user_json(self._store, self._username, CHAT_HISTORY_FILE, {})
        except Exception:  # noqa: BLE001 - 便利数据，读不出来就当没有
            return
        self._days = _normalize_history(payload)

    def _save_history(self) -> bool:
        """把谈话记录加密落盘。返回"真的写下去了没有"。"""
        if self._store is None or not self._username:
            return False
        try:
            from core.user_store import save_user_json

            save_user_json(
                self._store,
                self._username,
                CHAT_HISTORY_FILE,
                {"schema": HISTORY_SCHEMA, "days": self._days},
            )
        except Exception:  # noqa: BLE001 - 存不下不该打断谈话
            return False
        return True

    def _remember(self, role: str, text: str) -> None:
        """把一轮记进内存（按日期分组）。"""
        moment = _now()
        turn = {
            "role": role,
            "text": text,
            "time": _clock(moment),
            "model": self.model_combo.currentData() or "",
        }
        bucket = self._days.setdefault(_day_key(moment), [])
        bucket.append(turn)
        if len(bucket) > MAX_TURNS_PER_DAY:
            del bucket[: len(bucket) - MAX_TURNS_PER_DAY]

    def _render_history(self) -> None:
        """把记录画进左边那个只读框（纯文本，不带任何 Markdown 记号）。"""
        self.history_view.clear()
        for day in sorted(self._days):
            self.history_view.append(f"—— {day} ——")
            for turn in self._days[day]:
                speaker = _SPEAKER.get(turn.get("role", ""), "辅导员")
                clock = turn.get("time") or ""
                stamp = f"（{clock}）" if clock else ""
                self.history_view.append(f"{speaker}{stamp}：{turn.get('text', '')}")
            self.history_view.append("")
        self.history_view.moveCursor(self.history_view.textCursor().MoveOperation.End)

    def _print_line(self, text: str) -> None:
        """往记录框里追加一行并滚到底（一次性的整行用）。"""
        cursor = self.history_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        if not self.history_view.toPlainText().endswith("\n"):
            cursor.insertText("\n")
        cursor.insertText(text)
        self.history_view.setTextCursor(cursor)
        self.history_view.ensureCursorVisible()

    def _append_text(self, text: str) -> None:
        """在记录框末尾**续写**一段（不换行）—— 流式逐字显示的用。"""
        cursor = self.history_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self.history_view.setTextCursor(cursor)
        self.history_view.ensureCursorVisible()

    def _stream_answer(self, piece: str) -> None:
        """把刚吐出来的一块接到"你："那一行后面。

        ★ 第一块才写说话人、才换行；后面的块一律**原地续写** —— 每块都换行的话，
          界面上会变成一列竖着的单字，而且"你好呀"这种连续词再也拼不起来。
        """
        if not piece:
            return
        if not self._answer_open:
            self._answer_open = True
            self._print_line(f"{_SPEAKER[ROLE_ASSISTANT]}：{piece}")
            return
        self._append_text(piece)

    def _record_partial(self, worker: _ChatWorker | None) -> None:
        """把某个 worker 已经吐出来的回答记进历史（半途停下的那部分也算）。

        ★ 记的是**线程侧**攒的那份（``worker.answer``）：窗口关闭时排队的
          ``chunk`` 信号可能还没派发到主线程，只有线程自己那份是完整的。
          ``_ChatWorker.run()`` 结束前不会被回收，所以这里读到的是终值。
        """
        if worker is None:
            return
        answer = (worker.answer or "").strip()
        if not answer:
            return
        self._remember(ROLE_ASSISTANT, answer)

    # ------------------------------------------------------------ 提示词
    def _build_prompt(self) -> str:
        """系统提示词 + 最近若干轮 → 一段纯文本提示词。"""
        turns = self._history_turns()
        recent = turns[-(MAX_CONTEXT_TURNS + 1): -1] if len(turns) > 0 else []
        lines = [self._system_prompt, "", "下面是谈话记录（辅导员 = 用户，你 = 助手）："]
        body: list[str] = []
        for turn in recent:
            speaker = "辅导员" if turn.get("role") == ROLE_USER else "你"
            body.append(f"{speaker}：{turn.get('text', '')}")
        if turns:
            body.append(f"辅导员：{turns[-1].get('text', '')}")
        blob = "\n".join(body)
        if len(blob) > MAX_PROMPT_CHARS:
            blob = blob[-MAX_PROMPT_CHARS:]
            blob = blob.split("\n", 1)[-1]
        lines.append(blob)
        lines.append("")
        lines.append("请用中文、分点回答，不要输出 Markdown 记号。")
        lines.append("你：")
        return "\n".join(lines)

    # ---------------------------------------------------------------- 发送
    def _on_send_clicked(self) -> None:
        text = self.input_edit.text().strip()
        if not text:
            return
        self.input_edit.clear()
        self.submit(text)

    def submit(self, text: str) -> bool:
        """把一段文字当用户输入发出去，并开始生成。

        :func:`open_chat` 的 ``initial_prompt`` 走的就是这条路。

        Returns:
            True 表示已经开始生成；False 表示这次没发出去
            （引擎不可用 / 正在回答 / 文字是空的）。
        """
        text = (text or "").strip()
        if not text:
            return False
        if self._worker is not None and self._worker.isRunning():
            self.status_label.setText("上一条还在回答，等它写完再来。")
            return False
        if not self.input_edit.isEnabled():
            # 引擎不可用：状态行上已经有中文说明了，这里不再弹窗
            return False

        try:
            engine = self._engine_for_current()
        except InferenceUnavailable as exc:
            self.status_label.setText(str(exc))
            return False

        self._remember(ROLE_USER, text)
        self._print_line(f"辅导员：{text}")
        prompt = self._build_prompt()

        self._streaming = True
        self._answer_open = False
        self.send_button.setEnabled(False)
        self.input_edit.setEnabled(False)
        self.status_label.setText("正在生成，请稍候…")

        worker = _ChatWorker(
            engine,
            prompt,
            max_tokens=DEFAULT_MAX_TOKENS,
            temperature=DEFAULT_TEMPERATURE,
            stop=STOP_WORDS,
        )
        worker.chunk.connect(self._on_chunk)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()
        return True

    def _on_chunk(self, piece: str) -> None:
        if not self._streaming:
            return
        self._stream_answer(piece)

    def _on_failed(self, message: str) -> None:
        self._streaming = False
        self._last_error = message or "这次生成没有成功，可以再试一次。"
        self.status_label.setText(self._last_error)
        self.status_label.setObjectName("Warn")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _on_worker_finished(self) -> None:
        """生成结束：把已经拿到的那部分记进历史、写盘、恢复输入区。"""
        finished_worker = self.sender()
        if isinstance(finished_worker, QThread) and finished_worker is not self._worker:
            # ★ 上一轮线程迟到的收尾回调：它已经让位给新的一轮了，这里什么都别动 ——
            # 否则会把新一轮的 worker 当成"跑完了"回收掉，输入区也会被错误地解锁。
            finished_worker.deleteLater()
            return

        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.deleteLater()

        self._streaming = False
        # ★ 助手那一轮也要进历史（含半途失败时已经拿到的那部分）——
        #   否则记录里只剩辅导员自己说的话，换个窗口打开就像在自问自答。
        self._record_partial(worker)
        self._save_history()
        self._refresh_availability()
        if self._last_error:
            # 失败原因比"可以开始谈话"重要，压在可用状态之后贴上去
            self.status_label.setText(self._last_error)
            self.status_label.setObjectName("Warn")
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)
            self._last_error = ""
        if self.input_edit.isEnabled():
            self.input_edit.setFocus()

    # ---------------------------------------------------------------- 导出
    def _on_export_clicked(self) -> None:
        self.export_text()

    def export_text(self) -> str | None:
        """导出整份谈话记录到用户选的位置。返回写出的路径（取消返回 ``None``）。"""
        turns = self._history_turns()
        if not turns:
            self.status_label.setText("还没有谈话记录可以导出。")
            return None
        body = self.history_as_text()
        default = str(Path.home() / f"谈话记录_{_now().strftime('%Y%m%d')}.txt")
        path, _filter = QFileDialog.getSaveFileName(
            self, "导出谈话记录", default, "文本文件 (*.txt)"
        )
        if not path:
            return None
        try:
            Path(path).write_text(body, encoding="utf-8")
        except OSError as exc:
            self.status_label.setText(f"导出失败：{exc.strerror or '这个位置写不进去'}。")
            return None
        self.status_label.setText(f"已导出到 {path}")
        return path

    def history_as_text(self) -> str:
        """整份记录翻成纯文本（按日期分组，导出的就是它）。"""
        lines: list[str] = []
        for day in sorted(self._days):
            lines.append(f"== {day} ==")
            for turn in self._days[day]:
                speaker = _SPEAKER.get(turn.get("role", ""), "辅导员")
                clock = turn.get("time") or ""
                stamp = f"（{clock}）" if clock else ""
                lines.append(f"{speaker}{stamp}：{turn.get('text', '')}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    # ---------------------------------------------------------------- 收尾
    def messages(self) -> list[dict[str, str]]:
        """摊平后的谈话记录（测试与调用方读它，别去碰 ``_days``）。"""
        return self._history_turns()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
        """关窗：停掉还在跑的生成、把记录写盘。"""
        self._stop_worker()
        self._save_history()
        super().closeEvent(event)

    def _stop_worker(self, wait_ms: int = 3000) -> None:
        """请求停止后台生成，并等它收尾。

        ★ 等不到**也不许销毁它** —— 一个还在运行的 ``QThread`` 被回收会让
          进程直接消失（AGENTS.md 第 4k 节同族）。这种情况下把它交给
          模块级集合留着，跑完自动移除。
        """
        worker = self._worker
        self._worker = None
        self._streaming = False
        if worker is None:
            return
        worker.cancel()
        if not worker.isRunning():
            self._record_partial(worker)
            worker.deleteLater()
            return
        if worker.wait(wait_ms):
            # 线程真的停了：它自己攒的那份回答已经是终值，记下来别丢
            self._record_partial(worker)
            worker.deleteLater()
            return
        self._record_partial(worker)
        _LIVE_WORKERS.add(worker)

        def _drop(finished_worker: QThread = worker) -> None:
            _LIVE_WORKERS.discard(finished_worker)

        worker.finished.connect(_drop)


# ---------------------------------------------------------------------------
# 冻结接口（D 任务只认这一个）
# ---------------------------------------------------------------------------


def open_chat(
    parent: QWidget | None = None,
    *,
    system_prompt: str | None = None,
    initial_prompt: str | None = None,
    prefer_model: str | None = None,
) -> ChatDialog:
    """打开「AI 谈话助手」（**非模态**）。

    Args:
        parent: 父窗口（一般是主窗）。谈话记录从它身上取 ``_store`` / ``_username``。
        system_prompt: 覆盖系统提示词；不给就用 :data:`SYSTEM_PROMPT`。
        initial_prompt: 预置的一段话（例如侧写页刚生成好的提纲）。
            给了它就**立刻开始生成**，用户打开窗口就能看到回答在往外冒。
            允许传一份学生字典 —— 会先过 ``redact_student`` 脱敏。
        prefer_model: 想优先选中的模型名；命不中就退回第一个可用的。

    Returns:
        已经 ``show()`` 出来的对话框。**非模态**：调用方不必等它关闭。
    """
    dialog = ChatDialog(
        parent,
        system_prompt=system_prompt,
        prefer_model=prefer_model,
    )
    text = _coerce_prompt(initial_prompt)
    if text:
        dialog.submit(text)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
