"""本地 AI 推理引擎（v1.9 第二阶段 C1/C2）。

★ 四条硬约束（改这个文件之前先读）
==================================

1. **完全离线**：``main._network_policy_report`` 会静态扫 ``core/ai/*.py``，
   只要出现一行真正的网络库 import（``urllib`` / ``requests`` / ``socket`` /
   ``http`` 那一类）就 FAIL（AGENTS.md 第 13 / 28 节）。本模块**一个都没有**，
   而且以后也不许加 —— 要用网络就去 ``core/updater``。
2. **导入期不 import ``llama_cpp``**：本机没装它，而这个模块会被
   ``--selfcheck``、``pytest``、以及登录后的界面 import。真的要用引擎必须在
   :meth:`LocalModelEngine.load` 里才 import，装不上就抛
   :class:`InferenceUnavailable`（中文人话，见第 3 条）。
3. **引擎不可用绝不静默返回空串**：``generate`` / ``stream`` 一律抛
   :class:`InferenceUnavailable`。界面上"什么都没发生"是最难查的故障。
4. **零 Qt**：这个模块不 import PySide6 —— 它要能在没有 Qt 的环境里直接测，
   也要能被纯数据层（侧写、测试）复用。

为什么是"懒加载 + 常驻内存"
---------------------------

``llama_cpp`` 是几百 MB 的原生扩展，import 一次要几百毫秒；模型本身
1~3 GB，加载一次要几秒。所以：

* import 推迟到真正 ``load()`` 的那一刻（第 2 条）；
* 加载好的实例**常驻**在这个对象里，``generate`` / ``stream`` 反复用同一份
  （``load()`` 幂等），换模型才 ``unload()``。

界面上的用法（见 :mod:`ui.chat_dialog`）：把 ``load()`` + ``stream()`` 都放到
后台线程里，主线程只收信号 —— 加载模型几秒钟不动的界面会被当成死机。
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_N_CTX",
    "DEFAULT_TEMPERATURE",
    "ENGINE_MODULE",
    "ENGINE_INSTALLED_MESSAGE",
    "ENGINE_READY_MESSAGE",
    "InferenceUnavailable",
    "LocalModel",
    "LocalModelEngine",
    "NO_ENGINE_MESSAGE",
    "engine_available",
    "friendly_error",
    "list_local_models",
]

#: 引擎模块名。**只有这一个字符串**知道它叫什么，别处一律用常量。
ENGINE_MODULE = "llama_cpp"

#: 上下文窗口默认值。2048 够放"系统提示词 + 最近若干轮谈话"，
#: 再大内存占用跟着涨（KV cache 与 n_ctx 成正比）。
DEFAULT_N_CTX = 2048

#: 单次生成最多出多少 token。默认 512 大约是一屏中文。
DEFAULT_MAX_TOKENS = 512

#: 采样温度。0.7 是"能聊但不太飞"的常见折中。
DEFAULT_TEMPERATURE = 0.7

#: 没装引擎时给用户看的话。**逐字照抄任务书给的文案**，而且按铁律 9 的要求：
#: 人话，不带任何包管理指令（不许出现 pip / conda 那种"你去装一下"）。
NO_ENGINE_MESSAGE = (
    "本机没有安装本地推理引擎，AI 谈话助手暂时不可用。"
    "安装方法见 docs/DEV_SUMMARY.md 的 AI 推理一节。"
)

#: 引擎已经 import 过（真的加载过、或测试注入的假模块）时的说明。
ENGINE_READY_MESSAGE = "本地推理引擎已就绪。"

#: 引擎装好了、但还没加载模型时的说明。
ENGINE_INSTALLED_MESSAGE = "本机已安装本地推理引擎，第一次使用时会加载模型。"

#: 界面上"这个模型跑得动"的两档负载等级（AGENTS.md 第 31 节）。
#: ★ 拿不到等级时**一律列出**，见 :attr:`LocalModel.usable`。
USABLE_LEVEL_KEYS = frozenset({"easy", "tight"})
USABLE_LEVEL_WORDS = ("轻松", "勉强")

#: 错误文案里要抹掉的"包管理指令"关键词（照 ``core.excel_io.sanitize_message``
#: 的路子，AGENTS.md 第 27e 节）。原生扩展的报错经常直接甩一句
#: "please pip install ..."，那不该出现在辅导员的界面上。
_INSTRUCTION_MARKERS = (
    "pip install",
    "pip3 install",
    "conda install",
    "use pip",
    "missing optional dependency",
)


class InferenceUnavailable(RuntimeError):
    """本机用不了本地推理引擎（没装 / 没加载 / 模型文件不在 / 生成失败）。

    ★ 它是 :class:`RuntimeError` 的子类，调用方可以只按"这一次谈话不可用"
      来兜底，而不用去分辨底下到底是哪一层出的问题。
    """


# ---------------------------------------------------------------------------
# 探测：有没有引擎（只探测，不 import）
# ---------------------------------------------------------------------------


def engine_available() -> tuple[bool, str]:
    """本机能不能做本地推理，返回 ``(可用?, 中文说明)``。

    ★ **只探测、不 import**：``importlib.util.find_spec`` 只查模块查找路径，
      不执行模块代码 —— 所以这个函数在启动阶段、在没有装引擎的机器上
      都可以安全调用（AGENTS.md 第 13 节：import 期零副作用）。

    ``sys.modules`` 里已经有 ``llama_cpp`` 的（真的加载过，或测试注入的假模块）
    直接算可用；显式置成 ``None`` 的（Python 里表示"这个模块不存在"）算不可用。
    """
    module = sys.modules.get(ENGINE_MODULE)
    if module is not None:
        return True, ENGINE_READY_MESSAGE
    if ENGINE_MODULE in sys.modules:
        return False, NO_ENGINE_MESSAGE
    spec: Any = None
    try:
        spec = importlib.util.find_spec(ENGINE_MODULE)
    except (ImportError, AttributeError, TypeError, ValueError):
        # 有的环境里父包半残，find_spec 会抛。找不到就是找不到，不往上冒。
        spec = None
    if spec is None:
        return False, NO_ENGINE_MESSAGE
    return True, ENGINE_INSTALLED_MESSAGE


def _import_engine() -> Any:
    """真的把引擎模块拿进来（**唯一允许碰 import 的地方**）。

    Raises:
        InferenceUnavailable: 没装 / 装坏了。文案一律是人话。
    """
    module = sys.modules.get(ENGINE_MODULE)
    if module is not None:
        return module
    if ENGINE_MODULE in sys.modules:
        # 显式置 None：调用方明确说了"这里没有引擎"（测试就靠这个模拟没装）。
        raise InferenceUnavailable(NO_ENGINE_MESSAGE)
    try:
        import importlib

        return importlib.import_module(ENGINE_MODULE)
    except ImportError as exc:
        raise InferenceUnavailable(NO_ENGINE_MESSAGE) from exc
    except Exception as exc:  # noqa: BLE001 - 装了但 DLL/依赖坏了也是"用不了"
        raise InferenceUnavailable(
            f"本地推理引擎加载失败（{type(exc).__name__}）。"
            "这台机器上它暂时用不了，AI 谈话助手先放一放。"
        ) from exc


def friendly_error(exc: BaseException | str) -> str:
    """把引擎抛出来的异常翻成人话（**绝不带包管理指令**）。

    原生扩展的报错原文常常是英文 + 一句 "please pip install ..."。
    这里只保留异常类型名和一句中文说明；原文里若真带着安装指令，
    那半句就整段丢掉（AGENTS.md 第 27e 节的同一条道理）。
    """
    name = type(exc).__name__ if isinstance(exc, BaseException) else "错误"
    detail = str(exc).strip().splitlines()[0].strip() if str(exc).strip() else ""
    lowered = detail.lower()
    if any(marker in lowered for marker in _INSTRUCTION_MARKERS):
        detail = ""
    head = f"本地推理引擎出错了（{name}）。"
    tail = "可以再试一次，或者换一个更小的模型。"
    return f"{head}{detail + ' ' if detail else ''}{tail}"


# ---------------------------------------------------------------------------
# 本地模型清单（名字 / 路径 / 负载等级）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalModel:
    """一个**真的装在本机**的模型。

    ★ "装好了"的判据不在这个文件里 —— 它来自
      :func:`core.ai.model_registry.local_model_files`（磁盘上有一个非空的
      ``.gguf`` 才算装好）。这里只负责把它翻成界面能用的形状。
    """

    #: 账本 / 目录名（等于 ``models/<名字>/`` 的那个"名字"）。
    name: str
    #: 显示名。远端清单里有中文 label 就用它，没有就退回 ``name``。
    label: str
    #: ``.gguf`` 文件的完整路径。
    path: Path
    #: 文件字节数；读不到就是 0。
    size: int = 0
    #: 负载等级键：``"easy" / "tight" / "hard"``。**拿不到就是空串**。
    level: str = ""
    #: 负载等级的中文标签（``轻松 / 勉强 / 困难``）。拿不到就是空串。
    level_text: str = ""
    #: 引擎（B 任务的模型中心）给的一句话说明，界面拿它当气泡。
    level_note: str = ""

    @property
    def size_text(self) -> str:
        """人话体积，例 ``"1.1 GB"``。拿不到就返回空串。"""
        if self.size <= 0:
            return ""
        try:
            from core.ai.model_registry import human_size_bytes

            return human_size_bytes(self.size)
        except Exception:  # noqa: BLE001 - 显示用，坏了不影响谈话
            return ""

    @property
    def usable(self) -> bool:
        """这个模型值不值得列进「AI 谈话助手」的模型下拉框。

        * 拿得到等级：只认 :data:`USABLE_LEVEL_KEYS` 那两档（轻松 / 勉强）；
        * 拿不到等级（B 任务的接口还没就位）：**一律算可用** ——
          任务书的要求是"拿不到负载等级就列全部已装"，宁可多列不可漏列。
        """
        if not self.level:
            return True
        if self.level in USABLE_LEVEL_KEYS:
            return True
        blob = f"{self.level} {self.level_text}"
        return any(word in blob for word in USABLE_LEVEL_WORDS)

    @property
    def display(self) -> str:
        """下拉框里那一行。例 ``"Qwen2.5 3B（负载：轻松） 1.9 GB"``。"""
        parts = [self.label or self.name]
        if self.level_text:
            parts.append(f"（负载：{self.level_text}）")
        size = self.size_text
        if size:
            parts.append(f" {size}")
        return "".join(parts)


def _installed_files(dest_dir: str | Path | None = None) -> dict[str, Path]:
    """``{模型名: .gguf 路径}`` —— 一律**复用**既有函数，绝不自己拼路径。

    首选 :func:`core.ai.model_registry.local_model_files`（AGENTS.md 第 29 / 31
    节的判据真源）；它万一不可用，退回 :func:`core.ai.model_downloader.
    installed_models`。两个都拿不到就返回空 —— **不抛异常**：
    推理引擎这一侧不该因为"模型中心还在改"而整个打不开。
    """
    try:
        from core.ai.model_registry import local_model_files

        return dict(local_model_files(dest_dir))
    except Exception:  # noqa: BLE001 - 清单坏了不该拖垮谈话助手
        pass
    try:
        from core.ai.model_downloader import installed_models
    except Exception:  # noqa: BLE001
        return {}
    files: dict[str, Path] = {}
    for info in installed_models(dest_dir):
        path = getattr(info, "path", None)
        if path is not None:
            files[str(getattr(info, "name", ""))] = path
    return files


def _remote_meta(dest_dir: str | Path | None = None) -> dict[str, Any]:
    """本地模型对应的清单条目 ``{名字: RemoteModel}``（拿不到就是空表）。

    走 :func:`core.ai.model_registry.merged_with_local` —— 它本来就有
    "本机已有但远端清单没列"的兜底，正好是这里要的东西。
    """
    try:
        from core.ai.model_registry import merged_with_local

        merged = merged_with_local([], dest_dir=dest_dir)
    except Exception:  # noqa: BLE001
        return {}
    table: dict[str, Any] = {}
    for model in merged:
        key = getattr(model, "id", "")
        if isinstance(key, str) and key:
            table[key] = model
    return table


def _call_level_fn(fn: Any, model: Any) -> str:
    """按两种可能的签名问一次负载等级；问不出来就返回空串。"""
    for attempt in (lambda: fn(model), lambda: fn(model, ram_gb=None)):
        try:
            value = attempt()
        except TypeError:
            continue  # 签名不对，换下一种
        except Exception:  # noqa: BLE001 - B 任务的接口还在改，问不出来就算了
            return ""
        return value if isinstance(value, str) else ""
    return ""


def _probe_level(model: Any, registry: Any) -> tuple[str, str, str]:
    """尽力问出 ``(等级键, 中文标签, 说明)``；拿不到一律 ``("", "", "")``。

    ★ B 任务（模型中心）在并行给 ``model_registry`` 加 ``load_level`` /
      ``load_note``。这里全部用 ``getattr`` + ``try`` 兼容：
      **接口还没就位时只是不显示等级，绝不报错**（任务书原文要求）。
    """
    if model is None:
        return "", "", ""
    # 条目自己的说明（清单里的 note）先拿着 —— 哪怕等级问不出来，这句也还有用
    note = str(getattr(model, "note", "") or "")
    if registry is None:
        # 连 model_registry 都没导入成功：等级无从谈起，但说明照给
        return "", "", note
    labels = getattr(registry, "LOAD_LABELS", None)
    labels = labels if isinstance(labels, Mapping) else {}

    raw = ""
    for attr in ("load_level", "loadLevel", "level", "tier"):
        value = getattr(model, attr, "")
        if isinstance(value, str) and value:
            raw = value
            break
    if not raw:
        fn = getattr(registry, "load_level", None)
        if callable(fn):
            raw = _call_level_fn(fn, model)

    note_fn = getattr(registry, "load_note", None)
    if callable(note_fn):
        note = _call_level_fn(note_fn, model) or note

    if not raw:
        return "", "", note
    text = labels.get(raw, "")
    return raw, str(text or ""), note


def list_local_models(dest_dir: str | Path | None = None) -> list[LocalModel]:
    """本机**真的装好了**的模型清单（名字 / 路径 / 负载等级）。

    * 路径与"装没装好"完全复用既有函数（见 :func:`_installed_files`）；
    * 负载等级拿不到就不显示（见 :func:`_probe_level`）；
    * 目录是空的、模型中心的账本坏了、``dest_dir`` 不存在 —— 一律返回空表，
      绝不抛异常。界面靠"空表"去禁用输入区。
    """
    files = _installed_files(dest_dir)
    if not files:
        return []
    try:
        from core.ai import model_registry as registry
    except Exception:  # noqa: BLE001 - 退化成"没有等级信息"
        registry = None
    meta = _remote_meta(dest_dir)

    models: list[LocalModel] = []
    for name in sorted(files):
        path = Path(files[name])
        entry = meta.get(name)
        level, level_text, level_note = _probe_level(entry, registry)
        try:
            size = int(path.stat().st_size)
        except OSError:  # pragma: no cover - 刚扫到文件就没了
            size = 0
        label = str(getattr(entry, "label", "") or "") or name
        models.append(
            LocalModel(
                name=name,
                label=label,
                path=path,
                size=size,
                level=level,
                level_text=level_text,
                level_note=level_note,
            )
        )
    return models


# ---------------------------------------------------------------------------
# 引擎本体
# ---------------------------------------------------------------------------


def _choice_text(result: Any) -> str | None:
    """从一次生成结果里抠出文本。

    llama-cpp-python 的形状是 ``{"choices": [{"text": "..."}]}``；
    测试里的假模块可能直接给字符串。**抠不出来返回 ``None``**
    （调用方据此报"结果看不懂"，而不是静默给个空串）。
    """
    if isinstance(result, str):
        return result
    if not isinstance(result, Mapping):
        return None
    choices = result.get("choices")
    if isinstance(choices, (list, tuple)) and choices:
        first = choices[0]
        if isinstance(first, Mapping):
            text = first.get("text")
            return text if isinstance(text, str) else None
        if isinstance(first, str):
            return first
    text = result.get("text")
    return text if isinstance(text, str) else None


class LocalModelEngine:
    """一个常驻内存的本地模型（零 Qt、零网络）。

    Args:
        model_path: ``.gguf`` 文件路径。**只在这里存着**，``load()`` 才校验它。
        n_ctx: 上下文窗口。默认 :data:`DEFAULT_N_CTX`。
        n_threads: 线程数。``None`` 表示交给引擎自己按物理核数决定。
        chat_format: 传给引擎的对话模板名（有的模型需要显式指定）。

    ★ 生命周期：``load()`` 幂等 → ``generate()`` / ``stream()`` 反复用 →
    ``unload()`` 释放。没 ``load()`` 就生成是**明确的错误**，不是"返回空"。
    ``__del__`` / ``close()`` 在任何阶段都安全（半初始化状态下也不炸）。
    """

    def __init__(
        self,
        model_path: str | Path | None,
        *,
        n_ctx: int = DEFAULT_N_CTX,
        n_threads: int | None = None,
        chat_format: str | None = None,
    ) -> None:
        self._llama: Any = None
        self._module: Any = None
        try:
            self._model_path: Path | None = Path(model_path) if model_path else None
        except TypeError as exc:
            raise InferenceUnavailable(
                "这个模型路径看不懂，没法加载。请到「模型中心」重新选一个模型。"
            ) from exc
        self._n_ctx = int(n_ctx)
        self._n_threads = None if n_threads is None else int(n_threads)
        self._chat_format = chat_format or None

    # ------------------------------------------------------------- 只读属性
    @property
    def model_path(self) -> Path | None:
        """当前模型文件路径（没设就是 ``None``）。"""
        return getattr(self, "_model_path", None)

    @property
    def is_loaded(self) -> bool:
        """引擎实例在不在手上。"""
        return getattr(self, "_llama", None) is not None

    @property
    def n_ctx(self) -> int:
        return getattr(self, "_n_ctx", DEFAULT_N_CTX)

    @property
    def chat_format(self) -> str | None:
        return getattr(self, "_chat_format", None)

    # ----------------------------------------------------------------- 加载
    def load(self) -> Any:
        """把模型读进内存。**幂等**：已经加载了就直接返回原实例。

        Raises:
            InferenceUnavailable: 没装引擎 / 模型文件不在 / 引擎加载失败。
        """
        if getattr(self, "_llama", None) is not None:
            return self._llama

        module = _import_engine()

        path = self.model_path
        if path is None:
            raise InferenceUnavailable(
                "还没有选模型。请先在「模型中心」里下载一个模型，再回来谈话。"
            )
        if not path.exists():
            raise InferenceUnavailable(
                f"没有找到模型文件 {path.name}。"
                "它可能被移动或删掉了，请到「模型中心」里重新下载。"
            )

        kwargs: dict[str, Any] = {
            "model_path": str(path),
            "n_ctx": self.n_ctx,
            "verbose": False,
        }
        if self._n_threads:
            kwargs["n_threads"] = self._n_threads
        if self.chat_format:
            kwargs["chat_format"] = self.chat_format

        try:
            llama = module.Llama(**kwargs)
        except InferenceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - 原生扩展的异常类型五花八门
            raise InferenceUnavailable(friendly_error(exc)) from exc

        self._llama = llama
        self._module = module
        return llama

    # ----------------------------------------------------------------- 生成
    def generate(
        self,
        prompt: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        *,
        stop: Sequence[str] | None = None,
    ) -> str:
        """一次性生成完整回答。**没加载就抛** :class:`InferenceUnavailable`。

        ``stop`` 是可选的停止串（界面上用来在"用户："处截断）。
        """
        llama = self._require_loaded()
        kwargs = self._completion_kwargs(max_tokens, temperature, stop)
        kwargs["stream"] = False
        kwargs["echo"] = False
        try:
            result = llama(prompt, **kwargs)
        except InferenceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise InferenceUnavailable(friendly_error(exc)) from exc
        text = _choice_text(result)
        if text is None:
            raise InferenceUnavailable(
                "本地推理引擎返回了看不懂的结果，这次生成没有成功。"
            )
        return text

    def stream(
        self,
        prompt: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        *,
        stop: Sequence[str] | None = None,
    ) -> Iterator[str]:
        """逐 token 生成，**一块一块 yield**（界面靠它做打字机效果）。

        ★ "没加载"这个判断是**立刻**做的（不等到第一次 ``next()``），
          否则调用方拿到一个生成器、以为没事，错误要等界面开始消费才炸。
        """
        llama = self._require_loaded()
        return self._iter_chunks(llama, prompt, max_tokens, temperature, stop)

    # ----------------------------------------------------------------- 释放
    def unload(self) -> None:
        """放开模型引用（让原生那边能回收显存 / 内存）。可反复调用。"""
        llama = getattr(self, "_llama", None)
        self._llama = None
        self._module = None
        if llama is None:
            return
        close = getattr(llama, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - 释放失败不该往上冒
                pass

    def close(self) -> None:
        """:meth:`unload` 的别名（``with`` 之外的常见写法）。"""
        self.unload()

    def __del__(self) -> None:
        """半初始化状态（``__new__`` 之后没跑 ``__init__``）也不许炸。

        ★ 析构里的异常 Python 只会打一行 "Exception ignored in"，而那一行
          通常淹没在关闭窗口时的一堆输出里 —— 所以这里兜到底。
        """
        try:
            self.unload()
        except BaseException:  # noqa: BLE001 - 解释器退出途中什么都可能发生
            pass

    # ------------------------------------------------------------- 内部工具
    def _require_loaded(self) -> Any:
        llama = getattr(self, "_llama", None)
        if llama is None:
            raise InferenceUnavailable(
                "模型还没有加载，现在没法生成。请先调用 load()，"
                "或者到界面上重新选一次模型。"
            )
        return llama

    @staticmethod
    def _completion_kwargs(
        max_tokens: int,
        temperature: float,
        stop: Sequence[str] | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
        }
        if stop:
            kwargs["stop"] = list(stop)
        return kwargs

    def _iter_chunks(
        self,
        llama: Any,
        prompt: str,
        max_tokens: int,
        temperature: float,
        stop: Sequence[str] | None,
    ) -> Iterator[str]:
        """真正的流式循环（生成器）。异常一律翻成 :class:`InferenceUnavailable`。"""
        kwargs = self._completion_kwargs(max_tokens, temperature, stop)
        kwargs["stream"] = True
        kwargs["echo"] = False
        try:
            response = llama(prompt, **kwargs)
            for chunk in response:
                piece = _choice_text(chunk)
                if piece:
                    yield piece
        except InferenceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise InferenceUnavailable(friendly_error(exc)) from exc
