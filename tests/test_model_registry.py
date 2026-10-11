"""``core/ai/model_registry.py`` 的单元测试（纯数据层，不碰 Qt、不碰真网络）。

★ 本文件的两条纪律（改它之前先读）
==================================

1. **绝不碰真实用户目录**：凡是会读盘 / 落盘的调用，一律传
   ``dest_dir=tmp_path``（或它下面的 ``models``）。仓库红线写着
   ``%LOCALAPPDATA%\\SGMS`` 是用户真数据（AGENTS.md 第 3 / 29 节），
   所以本文件里**不会**出现"不带 dest_dir 的写盘调用"。
2. **只访问本机 http.server**：需要 HTTP 的用例起一个 ``127.0.0.1:0`` 的
   临时目录服务（端口由系统分配），跑完 ``shutdown()``；"连不上"那一条
   用一个"先绑 0 端口拿到号、立刻关掉"的确定没人监听的地址。

被测模块本身不 import Qt、import 期不发任何请求，所以这里可以放心地
把它当纯函数库测。
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, Iterator

import pytest

from core.ai import model_registry as mr
from core.ai.model_registry import (
    DEFAULT_GROUP,
    GITEE_RAW_MODELS,
    INSTALLED_FILE,
    INSTALLED_SCHEMA,
    LOAD_EASY,
    LOAD_HARD,
    LOAD_TIGHT,
    LOCAL_GROUP,
    SHA256_MIN_LEN,
    SIZE_TOLERANCE,
    STATE_INSTALLED,
    STATE_MISSING,
    STATE_OUTDATED,
    ModelChoice,
    RemoteModel,
    build_choices,
    check_memory,
    default_checked,
    describe_download_plan,
    fetch_model_list,
    forget_installed,
    group_choices,
    human_size_bytes,
    installed_file,
    installed_state,
    load_installed,
    local_model_files,
    local_ram_gb,
    merged_with_local,
    parse_model_list,
    record_installed,
    save_installed,
)

# ---------------------------------------------------------------------------
# 公共素材
# ---------------------------------------------------------------------------

#: 仓库根目录（``tests/`` 的上一级）。
REPO_ROOT = Path(__file__).resolve().parents[1]

#: 真实清单 —— 用本机 http server 原样吐出来喂给 :func:`fetch_model_list`。
MANIFEST_PATH = REPO_ROOT / "update" / "ai_models.json"

HEX64_A = "a" * 64
HEX64_B = "b" * 64

#: 真实清单里两条模型的体积（2026-10-08 核对过，见 update/ai_models.json）。
REAL_SIZE_1_5B = 1117320736
REAL_SIZE_3B = 2104932768


def _entry(**overrides: Any) -> dict[str, Any]:
    """造一条**一定合法**的清单条目，再用 ``overrides`` 改坏它。"""
    item: dict[str, Any] = {
        "id": "model-one",
        "label": "一号模型",
        "group": "推荐",
        "file": "model-one.gguf",
        "url": "https://example.com/model-one.gguf",
        "size": 1024,
        "sha256": HEX64_A,
        "minRamGb": 4,
        "defaultCheck": True,
        "note": "说明文字",
    }
    item.update(overrides)
    return item


def _without(item: dict[str, Any], *keys: str) -> dict[str, Any]:
    """删掉若干键 —— 测的是"字段真的缺失"，不是"字段是空串"。"""
    return {key: value for key, value in item.items() if key not in keys}


def _payload(*items: Any, schema: Any = INSTALLED_SCHEMA) -> dict[str, Any]:
    return {"schema": schema, "models": list(items)}


def _model(model_id: str = "model-one", **overrides: Any) -> RemoteModel:
    fields: dict[str, Any] = {
        "id": model_id,
        "url": "https://example.com/x.gguf",
        "size": 1024,
    }
    fields.update(overrides)
    return RemoteModel(**fields)


def _make_model_file(dest_dir: Path, name: str, content: bytes = b"GGUF-fake") -> Path:
    """在 ``dest_dir/<名字>/<名字>.gguf`` 里造一个非空模型文件。"""
    folder = dest_dir / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.gguf"
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# 本机 http server（只服务临时目录，绝不外连）
# ---------------------------------------------------------------------------


class _ModelHandler(SimpleHTTPRequestHandler):
    """静默的目录服务；额外提供 ``/boom*`` 这条"服务器 500"路径。"""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 基类参数名
        """不往 stderr 写访问日志，免得污染 pytest 输出。"""

    def do_GET(self) -> None:  # noqa: N802 - 基类方法名
        if self.path.startswith("/boom"):
            body = b"boom"
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


@pytest.fixture()
def model_site(tmp_path: Path) -> Iterator[str]:
    """本机临时目录服务：真实清单 + 坏 JSON + 空清单。"""
    root = tmp_path / "www"
    root.mkdir()
    assert MANIFEST_PATH.is_file(), f"仓库里应该有真实的清单文件：{MANIFEST_PATH}"
    (root / "ai_models.json").write_bytes(MANIFEST_PATH.read_bytes())
    (root / "bad.json").write_text("not json", encoding="utf-8")
    (root / "empty.json").write_text('{"schema": 1, "models": []}', encoding="utf-8")

    server = HTTPServer(("127.0.0.1", 0), partial(_ModelHandler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _unreachable_url() -> str:
    """一个"确定没人监听"的地址：绑一次 0 端口拿到号，立刻关掉。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return f"http://127.0.0.1:{port}/ai_models.json"


# ---------------------------------------------------------------------------
# 常量与小工具
# ---------------------------------------------------------------------------


def test_模块常量取值符合契约() -> None:
    assert GITEE_RAW_MODELS.startswith("https://gitee.com/"), "清单地址应该走 Gitee raw"
    assert GITEE_RAW_MODELS.endswith("update/ai_models.json"), "清单地址应指向 update/ai_models.json"
    assert SHA256_MIN_LEN == 64, "sha256 的合法长度是 64"
    assert DEFAULT_GROUP == "推荐", "默认分组名变了，界面置顶逻辑会跟着错"
    assert LOCAL_GROUP == "本机已有", "本机补进来的分组名变了"
    assert INSTALLED_FILE == "installed.json", "账本文件名变了"
    assert INSTALLED_SCHEMA == 1, "账本结构版本变了"
    assert (STATE_INSTALLED, STATE_MISSING, STATE_OUTDATED) == ("已装", "未装", "待更新"), "三种状态文案变了"
    assert SIZE_TOLERANCE == 0.01, "大小比对容差是 1%"


def test_RemoteModel默认值与display兜底() -> None:
    model = RemoteModel(id="only-id")
    assert model.url == "", "url 默认应该是空串"
    assert model.size == 0, "size 默认应该是 0"
    assert model.sha256 == "", "sha256 默认应该是空串"
    assert model.min_ram_gb == 0.0, "min_ram_gb 默认应该是 0.0"
    assert model.default_check is False, "default_check 默认应该是 False"
    assert model.note == "", "note 默认应该是空串"
    assert model.display == "only-id", "label 为空时应退回 id"
    assert model.size_text == "", "size 为 0 时体积文字应该是空串"
    assert model.group_or_default == DEFAULT_GROUP, "group 为空时应退回推荐组"
    assert model.has_checksum is False, "没有 sha256 时 has_checksum 必须是 False"


def test_RemoteModel是冻结的() -> None:
    model = RemoteModel(id="frozen")
    with pytest.raises(dataclasses.FrozenInstanceError):
        model.label = "改不动"  # type: ignore[misc]  # 冻结 dataclass，赋值必须抛


def test_human_size_bytes各种输入() -> None:
    cases: list[tuple[Any, str]] = [
        (0, ""),
        (-1, ""),
        (None, ""),
        ("abc", ""),
        ("", ""),
        (512, "512B"),
        (1023, "1023B"),
        (1024, "1.0KB"),
        (1536, "1.5KB"),
        (1048576, "1.0MB"),
        (1073741824, "1.0GB"),
        (1117320736, "1.0GB"),
        (3222253504, "3.0GB"),
        (1099511627776, "1.0TB"),
    ]
    for value, expected in cases:
        actual = human_size_bytes(value)
        assert actual == expected, f"{value!r} 的体积文字应该是 {expected!r}，实际 {actual!r}"


# ---------------------------------------------------------------------------
# parse_model_list
# ---------------------------------------------------------------------------


def test_parse_合法清单的条数与每个字段() -> None:
    payload = _payload(
        _entry(),
        _entry(
            id="model-two",
            label="",
            group="",
            file="model-two.gguf",
            url="https://example.com/dir/model-two.gguf?token=1",
            size=2048,
            sha256=HEX64_A.upper(),
            minRamGb=8,
            defaultCheck=False,
            note="",
        ),
    )
    models, warnings = parse_model_list(payload)
    assert warnings == [], f"合法清单不该有任何警告，实际：{warnings}"
    assert len(models) == 2, f"应该解析出 2 条，实际 {len(models)} 条"

    first, second = models
    assert first.id == "model-one", "id 应原样保留"
    assert first.label == "一号模型", "label 应原样保留"
    assert first.group == "推荐", "group 应原样保留"
    assert first.file == "model-one.gguf", "file 应原样保留"
    assert first.url == "https://example.com/model-one.gguf", "url 应原样保留"
    assert first.size == 1024, f"size 应该是 1024，实际 {first.size!r}"
    assert isinstance(first.size, int) and not isinstance(first.size, bool), "size 必须是真整数"
    assert first.sha256 == HEX64_A, "小写 64 位哈希应原样保留"
    assert first.min_ram_gb == 4.0, f"min_ram_gb 应该是 4.0，实际 {first.min_ram_gb!r}"
    assert first.default_check is True, "defaultCheck 应读成 True"
    assert first.note == "说明文字", "note 应原样保留"
    assert first.display == "一号模型", "display 优先用 label"
    assert first.size_text == "1.0KB", f"1024 字节应显示 1.0KB，实际 {first.size_text!r}"
    assert first.group_or_default == DEFAULT_GROUP, "分组应该就是推荐"
    assert first.has_checksum is True, "有哈希时 has_checksum 必须是 True"

    assert second.label == "model-two", "label 为空时应退回 id"
    assert second.display == "model-two", "display 应退回 id"
    assert second.group == "", "空 group 在字段上仍是空串"
    assert second.group_or_default == DEFAULT_GROUP, "空 group 归到推荐组"
    assert second.sha256 == HEX64_A, "大写 sha256 必须归一化成小写"
    assert second.default_check is False, "defaultCheck=False 应读成 False"
    assert second.size == 2048 and second.size_text == "2.0KB", "2048 字节应显示 2.0KB"


@pytest.mark.parametrize("bad", [None, 42, "x", [], [1, 2], True, 3.5])
def test_parse_顶层不是JSON对象(bad: Any) -> None:
    models, warnings = parse_model_list(bad)
    assert models == [], f"顶层不是对象时应返回空列表，实际 {models!r}"
    assert warnings == ["清单格式不对：顶层应该是一个 JSON 对象。"], f"警告文案不对：{warnings}"


def test_parse_缺少models字段() -> None:
    models, warnings = parse_model_list({"schema": INSTALLED_SCHEMA})
    assert models == [], "没有 models 字段就不该有模型"
    assert warnings == ["清单格式不对：models 字段应该是一个列表。"], f"警告文案不对：{warnings}"


@pytest.mark.parametrize("bad_models", [{"a": 1}, "abc", 42, None, True])
def test_parse_models不是列表(bad_models: Any) -> None:
    models, warnings = parse_model_list({"schema": INSTALLED_SCHEMA, "models": bad_models})
    assert models == [], f"models 不是列表时应返回空列表，实际 {models!r}"
    assert warnings == ["清单格式不对：models 字段应该是一个列表。"], f"警告文案不对：{warnings}"


def test_parse_缺url的条目跳过并逐条警告() -> None:
    payload = _payload(
        _without(_entry(), "url"),
        _entry(id="m2", url=""),
        _entry(id="m3", url="   "),
    )
    models, warnings = parse_model_list(payload)
    assert models == [], "三条都没有可用 url，应该一条都不剩"
    assert warnings == [
        "第 1 条缺少 url，已跳过。",
        "第 2 条缺少 url，已跳过。",
        "第 3 条缺少 url，已跳过。",
    ], f"警告文案或条数不对：{warnings}"


def test_parse_size非法一律跳过() -> None:
    payload = _payload(
        _entry(id="m1", size=0),
        _entry(id="m2", size=-5),
        _entry(id="m3", size="abc"),
        _entry(id="m4", size=None),
        _entry(id="m5", size=True),
        _entry(id="m6", size=1.5),
        _entry(id="m7", size="0"),
        _without(_entry(id="m8"), "size"),
    )
    models, warnings = parse_model_list(payload)
    assert models == [], "size 非正整数的条目一条都不该留下"
    expected = [
        f"第 {index} 条缺少可用的 size（要正整数），已跳过。" for index in range(1, 9)
    ]
    assert warnings == expected, f"警告文案或条数不对：{warnings}"


def test_parse_size的字符串与浮点写法被接受() -> None:
    payload = _payload(
        _entry(id="m1", size=2048),
        _entry(id="m2", size="2048"),
        _entry(id="m3", size=2.0),
        _entry(id="m4", size="  7  "),
    )
    models, warnings = parse_model_list(payload)
    assert warnings == [], f"这些写法都该被收下，不该有警告：{warnings}"
    assert [m.size for m in models] == [2048, 2048, 2, 7], "数字字符串 / 整数值浮点都应收敛成正整数"
    assert [m.size_text for m in models] == ["2.0KB", "2.0KB", "2B", "7B"], "体积文字应跟着 size 走"


def test_parse_id缺失时依次用file和url末段兜底() -> None:
    payload = _payload(
        _without(_entry(url="https://example.com/from-file.gguf"), "id"),
        _without(_without(_entry(url="https://example.com/dir/from-url.gguf"), "id"), "file"),
        _without(
            _without(_entry(url="https://example.com/a/b/q.gguf?v=1#frag"), "id"), "file"
        ),
        _without(_without(_entry(url="https://example.com/only-dir/"), "id"), "file"),
    )
    models, warnings = parse_model_list(payload)
    assert warnings == [], f"这四条都该能救活，不该有警告：{warnings}"
    assert [m.id for m in models] == [
        "model-one.gguf",
        "from-url.gguf",
        "q.gguf",
        "only-dir",
    ], "id 兜底顺序应该是 id → file → url 末段（且要剥掉 query / fragment / 结尾斜杠）"
    assert models[0].file == "model-one.gguf", "file 字段本身要保留"


def test_parse_从id与file与url都取不出名字时跳过() -> None:
    payload = _payload(_entry(id="", file="", url="/"))
    models, warnings = parse_model_list(payload)
    assert models == [], "三样都取不出名字的条目必须跳过"
    assert warnings == ["第 1 条从 id、file、url 都取不出名字，已跳过。"], f"警告文案不对：{warnings}"


def test_parse_重复id保留第一条() -> None:
    payload = _payload(
        _entry(label="第一条"),
        _entry(label="第二条"),
        _entry(id="model-two", label="另一条"),
    )
    models, warnings = parse_model_list(payload)
    assert [m.id for m in models] == ["model-one", "model-two"], "重复 id 只留第一条"
    assert models[0].label == "第一条", "留下的应该是先出现的那条"
    assert warnings == ["第 2 条的 id（model-one）和前面重复了，保留第一条。"], (
        f"重复 id 应该有且只有一句警告：{warnings}"
    )


def test_parse_sha256大写归一化成小写() -> None:
    models, warnings = parse_model_list(_payload(_entry(sha256=HEX64_A.upper())))
    assert warnings == [], f"大写哈希是合法的，不该有警告：{warnings}"
    assert models[0].sha256 == HEX64_A, f"应归一化成小写，实际 {models[0].sha256!r}"
    assert models[0].has_checksum is True, "归一化之后必须算「有校验和」"


def test_parse_sha256带前缀写法被接受() -> None:
    models, warnings = parse_model_list(_payload(_entry(sha256=f"sha256: {HEX64_A.upper()}")))
    assert warnings == [], f"``sha256: <hex>`` 是清单里常见的写法，不该警告：{warnings}"
    assert models[0].sha256 == HEX64_A, "前缀和大写都应被去掉"


@pytest.mark.parametrize("bad_sha", [HEX64_A[:63], "zz" * 32, "not-a-hash", HEX64_A + "0"])
def test_parse_sha256太短只警告仍保留条目(bad_sha: str) -> None:
    models, warnings = parse_model_list(_payload(_entry(sha256=bad_sha)))
    assert len(models) == 1, "哈希认不出来只该警告，不该把整条丢掉"
    assert models[0].sha256 == "", f"认不出来的哈希应收敛成空串，实际 {models[0].sha256!r}"
    assert models[0].has_checksum is False, "空哈希 = 没法校验完整性"
    assert warnings == [
        "第 1 条（model-one）的 sha256 不是 64 位十六进制，这条没法校验完整性。"
    ], f"警告文案不对：{warnings}"


def test_parse_缺少sha256只警告仍保留条目() -> None:
    models, warnings = parse_model_list(_payload(_without(_entry(), "sha256")))
    assert len(models) == 1, "没写 sha256 不该丢条目"
    assert models[0].sha256 == "", "缺哈希时字段是空串"
    assert warnings == ["第 1 条（model-one）没写 sha256，这条没法校验完整性。"], (
        f"警告文案不对：{warnings}"
    )


def test_parse_内存与默认勾选的驼峰别名() -> None:
    models, warnings = parse_model_list(
        _payload(_entry(minRamGb=8, defaultCheck=True))
    )
    assert warnings == [], f"合法条目不该有警告：{warnings}"
    assert models[0].min_ram_gb == 8.0, f"minRamGb 应读成 8.0，实际 {models[0].min_ram_gb!r}"
    assert models[0].default_check is True, "defaultCheck 应读成 True"


def test_parse_下划线别名可读且驼峰优先() -> None:
    payload = _payload(
        _without(_without(_entry(), "minRamGb", "defaultCheck"),)
        | {"min_ram_gb": 2.5, "default_check": "yes"},
        _entry(id="m2", minRamGb=8, min_ram_gb=1, defaultCheck=0),
        _entry(id="m3", minRamGb="4", defaultCheck="是"),
    )
    # 上一条的写法有点绕，这里显式说明：第 1 条是"只有下划线别名"的那条。
    payload["models"][0] = _without(_without(_entry(), "minRamGb"), "defaultCheck") | {
        "min_ram_gb": 2.5,
        "default_check": "yes",
    }
    models, warnings = parse_model_list(payload)
    assert warnings == [], f"下划线别名 / 容错写法都该被收下：{warnings}"
    assert models[0].min_ram_gb == 2.5, f"下划线别名 min_ram_gb 应读成 2.5，实际 {models[0].min_ram_gb!r}"
    assert models[0].default_check is True, '"yes" 这类字符串应被当成 True'
    assert models[1].min_ram_gb == 8.0, "驼峰和下划线同时出现时，驼峰优先"
    assert models[1].default_check is False, "defaultCheck=0 应被当成 False"
    assert models[2].min_ram_gb == 4.0, "字符串形式的 minRamGb 应被读成 4.0"
    assert models[2].default_check is True, '"是" 应被当成 True'


def test_parse_schema不匹配只警告不丢条目() -> None:
    models, warnings = parse_model_list(_payload(_entry(), schema=2))
    assert len(models) == 1, "schema 不认识也要按老格式尽力解析"
    assert warnings == ["清单的 schema 是 2，本版本只认 1，仍按老格式解析。"], (
        f"警告文案不对：{warnings}"
    )

    models2, warnings2 = parse_model_list({"models": [_entry()]})
    assert warnings2 == [], f"没有 schema 字段时不该警告：{warnings2}"
    assert len(models2) == 1, "没有 schema 字段不影响解析"


def test_parse_非对象条目跳过() -> None:
    payload = _payload(_entry(), "字符串", 42, [1], None)
    models, warnings = parse_model_list(payload)
    assert [m.id for m in models] == ["model-one"], "只有第 1 条是合法对象"
    assert warnings == [
        "第 2 条不是一个对象，已跳过。",
        "第 3 条不是一个对象，已跳过。",
        "第 4 条不是一个对象，已跳过。",
        "第 5 条不是一个对象，已跳过。",
    ], f"警告文案或条数不对：{warnings}"


def test_parse_空列表返回空且零警告() -> None:
    models, warnings = parse_model_list({"schema": INSTALLED_SCHEMA, "models": []})
    assert models == [], "空 models 应返回空列表"
    assert warnings == [], "空 models 不是错误，不该有警告"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        0,
        "",
        [],
        {},
        {"models": []},
        {"models": [None, 1, "x"]},
        {"models": [{"url": "u", "size": 1}]},
        {"schema": "1", "models": []},
        {"schema": None, "models": [{"url": "u", "size": "x"}]},
    ],
)
def test_parse_任何垃圾输入都不抛异常(payload: Any) -> None:
    result = parse_model_list(payload)
    assert isinstance(result, tuple) and len(result) == 2, "必须返回二元组"
    models, warnings = result
    assert isinstance(models, list), "第一个元素必须是 list"
    assert isinstance(warnings, list), "第二个元素必须是 list"
    assert all(isinstance(item, str) for item in warnings), "警告必须是字符串"


# ---------------------------------------------------------------------------
# fetch_model_list
# ---------------------------------------------------------------------------


def test_fetch_本机服务返回真实清单(model_site: str) -> None:
    models, warnings, status = fetch_model_list(f"{model_site}/ai_models.json", timeout=5)
    assert warnings == [], f"真实清单应当零警告，实际：{warnings}"
    assert len(models) >= 4, f"真实清单至少 4 条（v1.9 扩了模型集），实际 {len(models)} 条"
    assert status == f"已从远端取到 {len(models)} 个模型。", f"状态说明不对：{status}"

    for model in models:
        assert isinstance(model.size, int) and model.size > 0, f"{model.id} 的 size 必须是正整数"
        assert len(model.sha256) == SHA256_MIN_LEN, f"{model.id} 的 sha256 必须是 64 位"
        assert model.sha256 == model.sha256.lower(), f"{model.id} 的 sha256 应该是小写"
        assert model.url.startswith(("https://modelscope.cn/", "https://hf-mirror.com/")), (
            f"{model.id} 的直链只能来自 ModelScope 或 HF Mirror"
        )

    # ★ 不许只有单一来源：HF 直连在本机实测超时（WinError 10060），
    #   ChatGLM3 那条只能走 hf-mirror —— 这两条断言钉住"兜底源确实在用"。
    hosts = {model.url.split("/")[2] for model in models}
    assert hosts <= {"modelscope.cn", "hf-mirror.com"}, f"出现了计划外的来源：{sorted(hosts)}"
    assert "hf-mirror.com" in hosts, "至少要有 1 条走 hf-mirror 兜底源"

    by_id = {model.id: model for model in models}
    for legacy in ("qwen2.5-1.5b-instruct-q4_k_m", "qwen2.5-3b-instruct-q4_k_m"):
        assert legacy in by_id, f"{legacy} 是老清单就有的条目，不该消失"
    assert by_id["qwen2.5-1.5b-instruct-q4_k_m"].size == REAL_SIZE_1_5B, "1.5B 的体积和清单对不上"
    assert by_id["qwen2.5-3b-instruct-q4_k_m"].size == REAL_SIZE_3B, "3B 的体积和清单对不上"
    assert by_id["qwen2.5-1.5b-instruct-q4_k_m"].min_ram_gb == 4.0, "1.5B 的内存要求读错了"
    assert by_id["qwen2.5-3b-instruct-q4_k_m"].min_ram_gb == 8.0, "3B 的内存要求读错了"
    assert by_id["qwen2.5-1.5b-instruct-q4_k_m"].default_check is True, "1.5B 应当默认勾选"
    assert by_id["qwen2.5-3b-instruct-q4_k_m"].default_check is False, "3B 不该默认勾选"

    # 再和服务的那份文件本身对一遍：解析结果不许和 JSON 里写的漂移。
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["models"]
    assert [(m.id, m.size, m.sha256, m.url) for m in models] == [
        (item["id"], item["size"], item["sha256"], item["url"]) for item in raw
    ], "解析出来的字段必须和清单文件逐字一致"


def test_fetch_404不等于故障(model_site: str) -> None:
    models, warnings, status = fetch_model_list(f"{model_site}/not-published.json", timeout=5)
    assert models == [] and warnings == [], "404 时应返回空模型、空警告"
    assert status == "远端还没发布 update/ai_models.json（404 不是故障）。", f"状态说明不对：{status}"


def test_fetch_非JSON(model_site: str) -> None:
    models, warnings, status = fetch_model_list(f"{model_site}/bad.json", timeout=5)
    assert models == [] and warnings == [], "坏 JSON 时应返回空模型、空警告"
    assert status == "清单不是合法 JSON，这次没取到任何模型。", f"状态说明不对：{status}"


def test_fetch_空清单(model_site: str) -> None:
    models, warnings, status = fetch_model_list(f"{model_site}/empty.json", timeout=5)
    assert models == [] and warnings == [], "空清单应返回空模型、空警告"
    assert status == "清单取到了，但里面一条能用的模型都没有。", f"状态说明不对：{status}"


def test_fetch_连不上() -> None:
    models, warnings, status = fetch_model_list(_unreachable_url(), timeout=2)
    assert models == [] and warnings == [], "连不上时应返回空模型、空警告"
    assert status == "连不上服务器（可能是断网或 Gitee 抽风），稍后再试。", f"状态说明不对：{status}"


def test_fetch_HTTP500(model_site: str) -> None:
    models, warnings, status = fetch_model_list(f"{model_site}/boom.json", timeout=5)
    assert models == [] and warnings == [], "非 200 时应返回空模型、空警告"
    assert status == "服务器返回 HTTP 500，这次没取到清单。", f"状态说明不对：{status}"


def test_fetch_底层抛异常也不外泄(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(url: str, timeout: float) -> Any:
        raise RuntimeError("假装底层炸了")

    monkeypatch.setattr(mr, "_get_json", _boom)
    models, warnings, status = fetch_model_list("http://127.0.0.1:1/none.json", timeout=1)
    assert models == [] and warnings == [], "底层炸了也必须返回空结果，绝不能把异常漏给界面"
    assert status == "读取清单时出了意外（RuntimeError），这次先跳过。", f"兜底文案不对：{status!r}"


def test_fetch_六种说明互不相同且非空(model_site: str) -> None:
    statuses = [
        fetch_model_list(f"{model_site}/ai_models.json", timeout=5)[2],
        fetch_model_list(f"{model_site}/not-published.json", timeout=5)[2],
        fetch_model_list(f"{model_site}/boom.json", timeout=5)[2],
        fetch_model_list(f"{model_site}/bad.json", timeout=5)[2],
        fetch_model_list(f"{model_site}/empty.json", timeout=5)[2],
        fetch_model_list(_unreachable_url(), timeout=2)[2],
    ]
    assert all(text.strip() for text in statuses), f"每种状态的说明都必须非空：{statuses}"
    assert len(set(statuses)) == 6, f"六种状态必须有六句不同的话，实际：{statuses}"


# ---------------------------------------------------------------------------
# 账本：installed.json
# ---------------------------------------------------------------------------


def test_installed_file落在dest_dir下(tmp_path: Path) -> None:
    assert installed_file(tmp_path) == tmp_path / INSTALLED_FILE, "账本应落在 dest_dir 下"
    assert installed_file(tmp_path / "models") == tmp_path / "models" / INSTALLED_FILE, (
        "dest_dir 传子目录时应跟着走"
    )


def test_dest_dir为None时走default_models_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``dest_dir=None`` 那条路必须经过 ``default_models_dir()``。

    ★ 本用例把 ``default_models_dir`` 换成一个 tmp 目录，所以**仍然不会碰**
      ``%LOCALAPPDATA%\\SGMS``。这既补上了 ``dest_dir=None`` 分支的覆盖，
      也钉住了"默认目录只有一个出口"这个性质 —— 以后谁想绕开它自己拼路径，
      这条用例就会红。
    """
    import core.ai.model_downloader as downloader

    fake_root = tmp_path / "fake-sgms" / "models"
    monkeypatch.setattr(downloader, "default_models_dir", lambda: fake_root)

    assert installed_file(None) == fake_root / INSTALLED_FILE, "不带 dest_dir 时应落在 default_models_dir() 下"
    assert installed_file() == fake_root / INSTALLED_FILE, "省略参数与显式 None 必须一致"

    (fake_root / "alpha").mkdir(parents=True)
    (fake_root / "alpha" / "alpha.gguf").write_bytes(b"GGUF")
    assert sorted(local_model_files(None)) == ["alpha"], "不带 dest_dir 时应扫默认目录"
    assert load_installed(None) == {}, "这时候默认目录里还没有账本"


def test_record_installed后可读且字段正确(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    record_installed("模型甲", sha256=HEX64_A.upper(), size=1234, file="a.gguf", label="甲模型", dest_dir=dest)
    data = load_installed(dest)
    assert list(data) == ["模型甲"], f"账本键应该是模型 id，实际 {list(data)!r}"
    assert data["模型甲"] == {
        "sha256": HEX64_A,
        "size": 1234,
        "file": "a.gguf",
        "label": "甲模型",
    }, f"账本字段对不上：{data['模型甲']!r}"


def test_record_installed累计多条且空哈希允许(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    record_installed("甲", sha256=HEX64_A, size=1, file="a.gguf", label="", dest_dir=dest)
    record_installed("乙", sha256="", size=2, file="b.gguf", dest_dir=dest)
    data = load_installed(dest)
    assert sorted(data) == ["乙", "甲"], f"两条都该记下，实际 {sorted(data)!r}"
    assert data["乙"]["sha256"] == "", "清单没给哈希时允许记空串（将来会显示成待更新）"
    assert data["乙"]["label"] == "", "没给 label 时记空串"


def test_save_installed写出可读中文且无unicode转义(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    saved = save_installed(
        {"模型甲": {"sha256": HEX64_A, "size": 1, "file": "甲.gguf", "label": "推荐模型甲"}},
        dest_dir=dest,
    )
    path = dest / INSTALLED_FILE
    assert saved == path, f"save_installed 应返回写成的路径，实际 {saved}"
    text = path.read_text(encoding="utf-8")
    assert "\\u" not in text, "账本里出现了 \\uXXXX 转义，中文就不可读了"
    assert "模型甲" in text, "中文 id 必须原样出现"
    assert "甲.gguf" in text, "中文文件名必须原样出现"
    assert "推荐模型甲" in text, "中文 label 必须原样出现"
    payload = json.loads(text)
    assert payload["schema"] == INSTALLED_SCHEMA, "账本顶层必须带 schema"
    assert payload["models"]["模型甲"]["file"] == "甲.gguf", "读回来内容要对得上"
    assert text.endswith("\n"), "账本应以换行结尾"
    assert "\r\n" not in text, "账本应按 LF 写，不该被翻译成 CRLF"


def test_save_installed原子写不留tmp(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    save_installed({"a": {"sha256": "", "size": 1, "file": "a.gguf"}}, dest_dir=dest)
    assert (dest / INSTALLED_FILE).is_file(), "账本应该写出来了"
    assert not (dest / (INSTALLED_FILE + ".tmp")).exists(), "原子写完成后不该留下 .tmp 文件"


def test_load_installed坏文件返回空且文件还在(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    dest.mkdir()
    path = dest / INSTALLED_FILE
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    assert load_installed(dest) == {}, "坏账本应返回空字典"
    assert path.is_file(), "坏账本不许被悄悄删掉（它可能是用户唯一的线索）"
    assert path.read_text(encoding="utf-8") == "{ 这不是 JSON", "坏账本内容一个字节都不该动"


def test_load_installed非法编码返回空(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    dest.mkdir()
    path = dest / INSTALLED_FILE
    path.write_bytes(b"\xff\xfe\x00 not utf-8")
    assert load_installed(dest) == {}, "读不出 UTF-8 时返回空字典，而不是抛异常"
    assert path.is_file(), "读不了的文件同样不许删"


def test_load_installed文件不存在返回空(tmp_path: Path) -> None:
    dest = tmp_path / "not-there"
    assert load_installed(dest) == {}, "账本不存在时返回空字典"
    assert not dest.exists(), "只读地看一眼不该顺手建目录"


@pytest.mark.parametrize(
    "payload",
    [
        "[1, 2, 3]",
        '"just a string"',
        "123",
        '{"models": []}',
        '{"models": "x"}',
        '{"models": null}',
        '{"schema": 1}',
    ],
)
def test_load_installed结构不对返回空(tmp_path: Path, payload: str) -> None:
    dest = tmp_path / "models"
    dest.mkdir()
    (dest / INSTALLED_FILE).write_text(payload, encoding="utf-8")
    assert load_installed(dest) == {}, f"结构不对的账本应返回空字典：{payload}"


def test_load_installed跳过非字典条目(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    dest.mkdir()
    (dest / INSTALLED_FILE).write_text(
        json.dumps({"schema": 1, "models": {"好的": {"size": 1}, "坏的": 42, "空的": None}}),
        encoding="utf-8",
    )
    assert load_installed(dest) == {"好的": {"size": 1}}, "只收字典值，别的键悄悄跳过"


def test_forget_installed返回值与条目删除(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    record_installed("甲", sha256=HEX64_A, size=1, file="a.gguf", dest_dir=dest)
    record_installed("乙", sha256=HEX64_B, size=2, file="b.gguf", dest_dir=dest)
    assert forget_installed("甲", dest_dir=dest) is True, "本来有这条，应返回 True"
    assert sorted(load_installed(dest)) == ["乙"], "被忘掉的条目应该消失，别的条目必须留下"
    assert forget_installed("甲", dest_dir=dest) is False, "第二次忘掉同一条应返回 False"
    assert forget_installed("从来没记过", dest_dir=dest) is False, "不存在的条目应返回 False"


def test_forget_installed绝不删磁盘上的模型文件(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    content = b"GGUF" * 100
    model_file = _make_model_file(dest, "model-one", content)
    record_installed(
        "model-one", sha256=HEX64_A, size=len(content), file=model_file.name, dest_dir=dest
    )
    model = _model("model-one", sha256=HEX64_A)
    assert installed_state(model, dest) == STATE_INSTALLED, "动手之前它应该是已装"

    assert forget_installed("model-one", dest_dir=dest) is True, "应该能划掉账本条目"
    assert model_file.is_file(), "forget_installed 只动账本，绝不许删磁盘上的模型文件"
    assert model_file.read_bytes() == content, "磁盘文件内容必须一个字节都没变"
    assert installed_state(model, dest) == STATE_MISSING, "划掉账本之后状态回到未装"


# ---------------------------------------------------------------------------
# 状态判定
# ---------------------------------------------------------------------------


def test_state_有gguf但账本没记录是未装(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    path = _make_model_file(dest, "alpha")
    assert local_model_files(dest)["alpha"] == path, "磁盘扫描应找到那个 .gguf"
    assert installed_state(_model("alpha"), dest) == STATE_MISSING, "账本没记录 → 未装"
    assert _state_of(None, "alpha", dest) == STATE_MISSING, "不传账本时同样是未装"


def _state_of(model_sha: str | None, model_id: str, dest: Path) -> str:
    """直接喂 ``_state_from`` 的纯函数版本（不读盘，用手造的 files / ledger）。"""
    model = _model(model_id, sha256=model_sha or "")
    files = local_model_files(dest)
    ledger = load_installed(dest)
    return mr._state_from(model, files, ledger)


def test_state_账本哈希相同是已装(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    _make_model_file(dest, "alpha")
    record_installed("alpha", sha256=HEX64_A, size=8, file="alpha.gguf", dest_dir=dest)
    assert installed_state(_model("alpha", sha256=HEX64_A), dest) == STATE_INSTALLED, (
        "哈希一致 → 已装"
    )


def test_state_账本哈希大写也能比对(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    _make_model_file(dest, "alpha")
    save_installed(
        {"alpha": {"sha256": HEX64_A.upper(), "size": 8, "file": "alpha.gguf"}},
        dest_dir=dest,
    )
    assert installed_state(_model("alpha", sha256=HEX64_A), dest) == STATE_INSTALLED, (
        "账本里的大写哈希也要归一化后再比"
    )


def test_state_账本哈希不同是待更新(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    _make_model_file(dest, "alpha")
    record_installed("alpha", sha256=HEX64_A, size=8, file="alpha.gguf", dest_dir=dest)
    assert installed_state(_model("alpha", sha256=HEX64_B), dest) == STATE_OUTDATED, (
        "远端哈希变了 → 待更新"
    )
    assert _state_of(HEX64_B, "alpha", dest) == STATE_OUTDATED, "_state_from 的判据要一致"


def test_state_有一边没哈希就是已装(tmp_path: Path) -> None:
    dest_a = tmp_path / "a"
    _make_model_file(dest_a, "alpha")
    record_installed("alpha", sha256="", size=8, file="alpha.gguf", dest_dir=dest_a)
    assert installed_state(_model("alpha", sha256=HEX64_A), dest_a) == STATE_INSTALLED, (
        "账本没哈希 → 能确定的只有装过"
    )

    dest_b = tmp_path / "b"
    _make_model_file(dest_b, "alpha")
    record_installed("alpha", sha256=HEX64_A, size=8, file="alpha.gguf", dest_dir=dest_b)
    assert installed_state(_model("alpha", sha256=""), dest_b) == STATE_INSTALLED, (
        "清单没哈希 → 同样只能算已装"
    )

    dest_c = tmp_path / "c"
    _make_model_file(dest_c, "alpha")
    record_installed("alpha", sha256="", size=8, file="alpha.gguf", dest_dir=dest_c)
    assert installed_state(_model("alpha", sha256=""), dest_c) == STATE_INSTALLED, (
        "两边都没哈希 → 已装"
    )


def test_state_磁盘文件被删就回到未装(tmp_path: Path) -> None:
    import shutil

    dest = tmp_path / "models"
    _make_model_file(dest, "alpha")
    record_installed("alpha", sha256=HEX64_A, size=8, file="alpha.gguf", dest_dir=dest)
    model = _model("alpha", sha256=HEX64_A)
    assert installed_state(model, dest) == STATE_INSTALLED, "动手前是已装"
    shutil.rmtree(dest / "alpha")
    assert installed_state(model, dest) == STATE_MISSING, "磁盘是唯一真相：文件没了就是未装"


def test_state_空gguf不算装好(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    folder = dest / "alpha"
    folder.mkdir(parents=True)
    (folder / "alpha.gguf").write_bytes(b"")
    record_installed("alpha", sha256=HEX64_A, size=0, file="alpha.gguf", dest_dir=dest)
    assert local_model_files(dest) == {}, "空的 .gguf 不算装好（下载中途断电会留下它）"
    assert installed_state(_model("alpha", sha256=HEX64_A), dest) == STATE_MISSING, (
        "空文件 + 账本说装过 → 仍然是未装"
    )


def test_state_from纯函数判据(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    path = _make_model_file(dest, "alpha")
    files = {"alpha": path}
    model = _model("alpha", sha256=HEX64_A)
    assert mr._state_from(model, files, {}) == STATE_MISSING, "账本里没这条 → 未装"
    assert mr._state_from(model, files, {"alpha": "不是字典"}) == STATE_MISSING, (
        "账本值不是字典 → 当没记录"
    )
    assert mr._state_from(model, files, {"alpha": {"sha256": HEX64_A}}) == STATE_INSTALLED, (
        "哈希一致 → 已装"
    )
    assert mr._state_from(model, files, {"alpha": {"sha256": HEX64_B}}) == STATE_OUTDATED, (
        "哈希不一致 → 待更新"
    )
    assert mr._state_from(model, {}, {"alpha": {"sha256": HEX64_A}}) == STATE_MISSING, (
        "磁盘上没有这个 id → 未装，哪怕账本写着"
    )


def test_local_model_files只认非空gguf且跳过temp(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    alpha = _make_model_file(dest, "alpha")
    beta_file = _make_model_file(dest, "beta")
    (dest / "temp").mkdir()
    (dest / "temp" / "half.gguf").write_bytes("半截".encode("utf-8"))
    (dest / "gamma").mkdir()
    (dest / "gamma" / "gamma.txt").write_bytes("不是模型".encode("utf-8"))
    (dest / "delta").mkdir()

    files = local_model_files(dest)
    assert sorted(files) == ["alpha", "beta"], f"只该认出这两个模型，实际 {sorted(files)!r}"
    assert files["alpha"] == alpha, "alpha 的路径不对"
    assert files["beta"] == beta_file, "beta 的路径不对"
    assert local_model_files(tmp_path / "不存在") == {}, "目录不存在时返回空字典"


# ---------------------------------------------------------------------------
# 内存
# ---------------------------------------------------------------------------


def test_check_memory_够() -> None:
    ok, note = check_memory(_model("a", min_ram_gb=8), ram_gb=16)
    assert ok is True, "16GB 跑 8GB 要求应当放行"
    assert note == "本机内存 16.0 GB，够跑（需要 8 GB）", f"说明文案不对：{note!r}"


def test_check_memory_不够() -> None:
    ok, note = check_memory(_model("a", min_ram_gb=8), ram_gb=4)
    assert ok is False, "4GB 跑 8GB 要求必须拦住"
    assert note == "内存不足：本机 4.0 GB，需要 8 GB", f"说明文案不对：{note!r}"


def test_check_memory_刚好等于需求也算够() -> None:
    ok, note = check_memory(_model("a", min_ram_gb=4), ram_gb=4)
    assert ok is True, "刚好等于要求算够（容差 1e-9）"
    assert note == "本机内存 4.0 GB，够跑（需要 4 GB）", f"说明文案不对：{note!r}"


def test_check_memory_没声明需求时不探测内存(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> float | None:
        raise AssertionError("min_ram_gb <= 0 时不该去探测内存")

    monkeypatch.setattr(mr, "local_ram_gb", _boom)
    for need in (0, 0.0, -3):
        ok, note = check_memory(_model("a", min_ram_gb=need), ram_gb=None)
        assert ok is True, f"没声明内存要求（{need}）必须放行"
        assert note == "这个模型没有声明内存要求", f"说明文案不对：{note!r}"


def test_check_memory_探测不到时先放行(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mr, "local_ram_gb", lambda: None)
    ok, note = check_memory(_model("a", min_ram_gb=64), ram_gb=None)
    assert ok is True, "测不到内存时必须放行，宁可让用户自己判断"
    assert note == "没测到本机内存，先放行（装之前自己看一眼内存）", f"说明文案不对：{note!r}"


def test_check_memory_ram_gb为None时用真实探测() -> None:
    have = local_ram_gb()
    ok, note = check_memory(_model("a", min_ram_gb=0.25), ram_gb=None)
    assert ok is True, "0.25GB 的要求在任何真机上都该放行"
    if have is None:
        assert note == "没测到本机内存，先放行（装之前自己看一眼内存）", f"说明文案不对：{note!r}"
    else:
        assert note == f"本机内存 {have:.1f} GB，够跑（需要 0.25 GB）", f"说明文案不对：{note!r}"


def test_default_checked只勾轻松档不看清单的默认标记() -> None:
    """★ v1.9 B4 的语义变更（``AGENTS.md`` 第 33 节）：默认勾选只看"本机跑它轻松"。

    清单里的 ``defaultCheck`` **不再参与**这个判断（它现在只用来给「推荐」角标）；
    所以下面 ``b`` 明明写着 ``defaultCheck=False``，本机跑得轻松时照样被预勾。
    """
    models = [
        _model("a", default_check=True, min_ram_gb=4),
        _model("b", default_check=False, min_ram_gb=4),
        _model("c", default_check=True, min_ram_gb=64),
    ]
    # 8GB：a / b 轻松（8 ≥ 4），c 困难（8 < 64 × 0.7 = 44.8）→ 只勾 a、b。
    assert default_checked(models, ram_gb=8) == ["a", "b"], (
        "按轻松档筛，不看清单的 defaultCheck（b 没标默认也照样勾上）"
    )
    assert default_checked(models, ram_gb=128) == ["a", "b", "c"], "128GB 上 c 也轻松了"
    assert default_checked(models, ram_gb=3) == [], (
        "3GB：a / b 只到「勉强」（3 ≥ 4 × 0.7 = 2.8），不预勾"
    )
    assert default_checked(models, ram_gb=1) == [], "内存不够时一条都不勾（困难）"
    assert default_checked([], ram_gb=8) == [], "空输入返回空列表"


@pytest.mark.skipif(os.name != "nt", reason="GlobalMemoryStatusEx 只在 Windows 上有")
def test_local_ram_gb_windows返回正浮点() -> None:
    value = local_ram_gb()
    assert isinstance(value, float), f"Windows 上应该返回 float，实际 {type(value)!r}"
    assert value > 0, f"物理内存总量必须大于 0，实际 {value!r}"


@pytest.mark.skipif(os.name != "nt", reason="GlobalMemoryStatusEx 只在 Windows 上有")
def test_local_ram_gb_系统调用失败时返回None(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mr.ctypes.windll.kernel32, "GlobalMemoryStatusEx", lambda *_: 0)
    assert local_ram_gb() is None, "系统调用失败时返回 None（= 我不知道），而不是编一个数"
    ok, note = check_memory(_model("big", min_ram_gb=64), ram_gb=None)
    assert ok is True and note == "没测到本机内存，先放行（装之前自己看一眼内存）", (
        f"探测失败时应当放行，实际 {ok!r} / {note!r}"
    )


def test_local_ram_gb_非windows返回None(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeOs:
        name = "posix"

    monkeypatch.setattr(mr, "os", _FakeOs)
    assert local_ram_gb() is None, "非 Windows 平台返回 None（= 我不知道，一律放行）"


# ---------------------------------------------------------------------------
# 合成：ModelChoice / build_choices / 分组 / 合并 / 汇总
# ---------------------------------------------------------------------------


def test_build_choices内存不足的行不可勾选(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    big = _model("big", min_ram_gb=8, default_check=True, url="https://example.com/big.gguf")
    small = _model("small", min_ram_gb=4, default_check=True, url="https://example.com/small.gguf")
    choices = build_choices([big, small], dest_dir=dest, ram_gb=4)

    assert [c.model.id for c in choices] == ["big", "small"], "顺序必须和入参一致"
    heavy, light = choices
    assert heavy.memory_ok is False, "8GB 要求在 4GB 机器上必须判成不够"
    assert heavy.memory_note == "内存不足：本机 4.0 GB，需要 8 GB", f"说明文案不对：{heavy.memory_note!r}"
    assert heavy.checked is False, "内存不够的行不该被默认勾上"
    assert heavy.selectable is False, "内存不够的行不可勾选"
    assert heavy.checked_by_default is False, "8GB 要求在 4GB 机器上是「困难」→ 不预勾"
    assert heavy.state == STATE_MISSING, "磁盘上没有它 → 未装"

    assert light.memory_ok is True, "4GB 要求在 4GB 机器上算够"
    assert light.checked is True, "轻松档（4 ≥ 4）的那条应当勾上"
    assert light.selectable is True, "内存够的行可勾选"
    assert light.checked_by_default is True, "轻松档 → 默认勾选（不看清单的 defaultCheck）"


def test_build_choices状态跟着账本走(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    _make_model_file(dest, "alpha")
    record_installed("alpha", sha256=HEX64_A, size=8, file="alpha.gguf", dest_dir=dest)
    choices = build_choices(
        [_model("alpha", sha256=HEX64_A), _model("beta", sha256=HEX64_B)],
        dest_dir=dest,
        ram_gb=8,
    )
    assert choices[0].state == STATE_INSTALLED, "哈希一致 → 已装"
    assert choices[1].state == STATE_MISSING, "磁盘上没有 beta → 未装"


def test_build_choices内存只探测一次(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def _fake() -> float:
        calls.append(1)
        return 16.0

    monkeypatch.setattr(mr, "local_ram_gb", _fake)
    models = [_model(f"m{index}", min_ram_gb=8) for index in range(5)]
    choices = build_choices(models, dest_dir=tmp_path / "models", ram_gb=None)
    assert len(calls) == 1, f"一批 5 条只该探测 1 次，实际探测 {len(calls)} 次"
    assert all(c.memory_ok for c in choices), "16GB 机器上 8GB 的要求都该放行"


def test_ModelChoice_checked_by_default只看负载等级() -> None:
    """``checked_by_default`` 只认负载三档里的 **轻松**（``AGENTS.md`` 第 33 节）。"""
    plain = ModelChoice(model=_model("a"))
    assert plain.state == STATE_MISSING, "state 默认是未装"
    assert plain.memory_ok is True, "memory_ok 默认是 True"
    assert plain.memory_note == "", "memory_note 默认是空串"
    assert plain.checked is False, "checked 默认是 False（那是「当前状态」）"
    assert plain.load_level == LOAD_EASY, "load_level 默认是轻松"
    assert plain.checked_by_default is True, (
        "轻松档 → 默认勾选（★ 与清单的 defaultCheck 无关）"
    )
    assert plain.selectable is True, "内存默认够 → 可勾选"

    wanted = ModelChoice(model=_model("b", default_check=True))
    assert wanted.checked_by_default is True, "清单标了默认 + 轻松档 → True"
    assert wanted.checked is False, "checked 和 checked_by_default 是两件事，构造时不联动"

    tight = ModelChoice(model=_model("c", default_check=True), load_level=LOAD_TIGHT)
    assert tight.checked_by_default is False, "「勉强」不预勾，哪怕清单标了 defaultCheck"
    assert tight.selectable is True, "「勉强」不等于不可选 —— 用户有权自己勾"

    blocked = ModelChoice(
        model=_model("d", default_check=True), load_level=LOAD_HARD, memory_ok=False
    )
    assert blocked.checked_by_default is False, "「困难」不预勾"
    assert blocked.selectable is False, "困难必然同时没过内存闸门 → 不可勾选"


def test_ModelChoice_status_text_内存不足原因优先() -> None:
    remote = _model("a", url="https://example.com/a.gguf")
    assert ModelChoice(model=remote, state=STATE_OUTDATED).status_text == STATE_OUTDATED, (
        "内存够时状态列显示安装状态"
    )
    note = "内存不足：本机 4.0 GB，需要 8 GB"
    blocked = ModelChoice(model=remote, state=STATE_OUTDATED, memory_ok=False, memory_note=note)
    assert blocked.status_text == note, "内存不足时原因优先于安装状态"
    silent = ModelChoice(model=remote, memory_ok=False)
    assert silent.status_text == "内存不足", "没有说明时的兜底文案是「内存不足」"


def test_ModelChoice_status_text_本机模型显示本机已有() -> None:
    local = ModelChoice(model=_model("local", url="", group=LOCAL_GROUP))
    assert local.status_text == LOCAL_GROUP, "没有下载地址的本机模型，状态列直接显示分组名"


def test_group_choices推荐组永远第一() -> None:
    choices = [
        ModelChoice(model=_model("h1", group="高级")),
        ModelChoice(model=_model("r1", group="")),
        ModelChoice(model=_model("x1", group="其他")),
        ModelChoice(model=_model("r2", group=DEFAULT_GROUP)),
    ]
    groups = group_choices(choices)
    assert [name for name, _ in groups] == [DEFAULT_GROUP, "高级", "其他"], (
        f"推荐组必须排第一、其余按出现顺序，实际 {[name for name, _ in groups]!r}"
    )
    assert [c.model.id for c in dict(groups)[DEFAULT_GROUP]] == ["r1", "r2"], (
        "没写 group 的条目归到推荐组，且保持原顺序"
    )
    assert [c.model.id for c in dict(groups)["高级"]] == ["h1"], "高级组里只有 h1"


def test_group_choices无推荐组时保持出现顺序() -> None:
    choices = [
        ModelChoice(model=_model("a", group="甲")),
        ModelChoice(model=_model("b", group="乙")),
        ModelChoice(model=_model("a2", group="甲")),
    ]
    groups = group_choices(choices)
    assert [name for name, _ in groups] == ["甲", "乙"], "没有推荐组时不该凭空插一个"
    assert [c.model.id for c in dict(groups)["甲"]] == ["a", "a2"], "同组内保持出现顺序"
    assert group_choices([]) == [], "空输入返回空列表"


def test_merged_with_local补进本机已有模型(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    local_file = _make_model_file(dest, "local-aaa", b"x" * 2048)
    remote = _model("remote-one", url="https://example.com/r.gguf", size=100)

    merged = merged_with_local([remote], dest_dir=dest)
    assert [m.id for m in merged] == ["remote-one", "local-aaa"], "远端清单在前，本机补进来的在后"
    extra = merged[1]
    assert extra.url == "", "本机补进来的条目没有可下地址"
    assert extra.group == LOCAL_GROUP, f"分组应该是 {LOCAL_GROUP}，实际 {extra.group!r}"
    assert extra.group_or_default == LOCAL_GROUP, "group_or_default 也跟着走"
    assert extra.label == "local-aaa", "名字就用目录名"
    assert extra.file == "local-aaa.gguf", "落盘文件名取自磁盘"
    assert extra.size == 2048, f"体积应该取自磁盘文件，实际 {extra.size!r}"
    assert extra.sha256 == "", "本机补进来的没有哈希"
    assert extra.has_checksum is False, "没有哈希 → 没法校验完整性"
    assert extra.min_ram_gb == 0.0, "本机已有的不声明内存要求"
    assert extra.default_check is False, "本机已有的东西不需要再下一次"
    assert extra.note == "这个模型在本机目录里，但远端清单没有列出它。", f"说明文案不对：{extra.note!r}"
    assert local_file.is_file(), "合并不许动磁盘"
    assert ModelChoice(model=extra).status_text == LOCAL_GROUP, "界面状态列显示「本机已有」"


def test_merged_with_local不重复远端已列的id(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    _make_model_file(dest, "remote-one")
    remote = _model("remote-one", url="https://example.com/r.gguf", size=100)
    merged = merged_with_local([remote], dest_dir=dest)
    assert [m.id for m in merged] == ["remote-one"], "和远端 id 撞名的本机目录不该重复出现"
    assert merged[0] is remote, "留下的应该是远端那条（它才有下载地址）"


def test_merged_with_local多个本机模型按名字排序(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    _make_model_file(dest, "zzz-local")
    _make_model_file(dest, "aaa-local")
    merged = merged_with_local([], dest_dir=dest)
    assert [m.id for m in merged] == ["aaa-local", "zzz-local"], "本机补进来的按目录名排序"
    assert merged_with_local([], dest_dir=tmp_path / "空目录") == [], "没有本机模型时原样返回空列表"


def test_describe_download_plan没勾选() -> None:
    assert describe_download_plan([]) == "还没有勾选任何模型", "一个都没勾时应说「没选」"
    unchecked = ModelChoice(model=_model("a", url="https://example.com/a.gguf", size=1024))
    assert describe_download_plan([unchecked]) == "还没有勾选任何模型", "没勾上的不算"


def test_describe_download_plan勾一个说出体积() -> None:
    model = _model("a", url="https://example.com/a.gguf", size=REAL_SIZE_1_5B)
    assert human_size_bytes(REAL_SIZE_1_5B) == "1.0GB", "先确认体积文字的底数"
    text = describe_download_plan([ModelChoice(model=model, checked=True)])
    assert text == "已选 1 个模型，共约 1.0GB", f"汇总文案不对：{text!r}"


def test_describe_download_plan勾两个是体积之和() -> None:
    first = ModelChoice(model=_model("a", url="u", size=REAL_SIZE_1_5B), checked=True)
    second = ModelChoice(model=_model("b", url="u", size=REAL_SIZE_3B), checked=True)
    assert human_size_bytes(REAL_SIZE_1_5B + REAL_SIZE_3B) == "3.0GB", "两条加起来是 3.0GB"
    text = describe_download_plan([first, second])
    assert text == "已选 2 个模型，共约 3.0GB", f"汇总文案不对：{text!r}"


def test_describe_download_plan忽略没有下载地址的本机模型() -> None:
    local = ModelChoice(model=_model("local", url="", size=10 ** 9), checked=True)
    assert describe_download_plan([local]) == "还没有勾选任何模型", (
        "本机已有的条目没有可下地址，不该被算进下载计划"
    )
    remote = ModelChoice(model=_model("a", url="u", size=REAL_SIZE_1_5B), checked=True)
    text = describe_download_plan([local, remote])
    assert text == "已选 1 个模型，共约 1.0GB", f"只该算有地址的那条：{text!r}"


def test_describe_download_plan与build_choices联动(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    models = [
        _model("a", url="https://example.com/a.gguf", size=1024, default_check=True, min_ram_gb=4),
        _model("b", url="https://example.com/b.gguf", size=1024, default_check=True, min_ram_gb=64),
    ]
    choices = build_choices(models, dest_dir=dest, ram_gb=8)
    assert [c.checked for c in choices] == [True, False], "默认勾选由 build_choices 初始化"
    assert describe_download_plan(choices) == "已选 1 个模型，共约 1.0KB", (
        f"汇总应只算默认勾上且跑得动的那条：{describe_download_plan(choices)!r}"
    )


def test_merged_with_local与build_choices串起来(tmp_path: Path) -> None:
    """端到端一小段：远端清单 → 补本机 → 合成界面行 → 汇总下载计划。"""
    dest = tmp_path / "models"
    _make_model_file(dest, "本机模型", b"y" * 4096)
    merged = merged_with_local(
        [_model("远端模型", url="https://example.com/r.gguf", size=1024, default_check=True)],
        dest_dir=dest,
    )
    choices = build_choices(merged, dest_dir=dest, ram_gb=8)
    assert [c.model.id for c in choices] == ["远端模型", "本机模型"], "两条都要在界面上出现"
    assert choices[0].checked is True, "远端那条默认勾上"
    assert choices[1].checked is False, "本机那条不参与默认勾选"
    assert describe_download_plan(choices) == "已选 1 个模型，共约 1.0KB", (
        f"汇总文案不对：{describe_download_plan(choices)!r}"
    )
    assert choices[1].status_text == LOCAL_GROUP, "本机那条的状态列显示「本机已有」"
