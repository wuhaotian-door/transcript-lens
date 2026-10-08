# -*- coding: utf-8 -*-
"""更新清单（``update/manifest.json``）与 :func:`core.updater.check_for_update`。

v1.9 把"最新版本号 + 主程序直链 / 体积 / sha256"从**发行版正文**搬进了结构化清单，
所以这里盯两件事：

1. 清单解析（:func:`core.updater.parse_manifest`）—— 格式好、格式坏、
   缺字段，都得给对答案，而且**永不抛异常**；
2. 清单优先、正文兜底（:func:`core.updater.check_for_update`）——
   清单能用就走清单；清单不能用（没发布 / 404 / 读不懂 / **缺三件套**）
   必须**原样退回** v1.8 的老路子。宁可让用户看到老行为，
   也绝不拿一条没校验的直链去替换程序。

★ 全程**不联网**：要么打本机 ``http.server``，要么把 ``core.updater._get_json``
  换成假的。真联网的测试在断网的机器上会变红，那种红是噪音而不是发现。
"""

from __future__ import annotations

import contextlib
import http.server
import json
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from core import updater as U

#: 仓库根（``update/manifest.json`` 就在它下面）
ROOT = Path(__file__).resolve().parents[1]

#: 一个**完整可用**的清单（测试里到处用，所以抽出来）
GOOD_PAYLOAD: dict[str, Any] = {
    "schema": 1,
    "version": "1.9",
    "published": "2026-10-08",
    "app": {
        "url": "https://example.com/transcript-lens_v1.9.exe",
        "size": 234567890,
        "sha256": "A1" * 32,
        "notes": "这一版修了解析器，加了模型中心。",
    },
}


def _payload(**overrides: Any) -> dict[str, Any]:
    """复制一份好清单，按需覆盖 ``app`` 段里的字段。"""
    data = json.loads(json.dumps(GOOD_PAYLOAD))
    app_over = overrides.pop("app", None)
    data.update(overrides)
    if app_over is not None:
        data["app"].update(app_over)
    return data


# ---------------------------------------------------------------------------
# 本机 http.server 脚手架
# ---------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    """把 ``files`` 里的 ``路径 -> (状态码, 字节, content-type)`` 吐出去。"""

    files: dict[str, tuple[int, bytes, str]] = {}

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口
        status, body, ctype = self.files.get(
            self.path, (404, b"not found", "text/plain")
        )
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # 别往 stderr 刷访问日志
        return


@contextlib.contextmanager
def serve(files: dict[str, tuple[int, bytes, str]]):
    """起一个本机服务器，yield 出它的根地址（端口由系统分配）。"""
    handler = type("_ServingHandler", (_Handler,), {"files": files})
    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def dead_url() -> str:
    """一个**确定没人监听**的本机地址（先占端口拿到号，再立刻关掉）。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}/update/manifest.json"


def json_file(payload: Any) -> tuple[int, bytes, str]:
    return (200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json")


# ---------------------------------------------------------------------------
# parse_manifest
# ---------------------------------------------------------------------------


def test_parse_manifest_好清单三件套齐全() -> None:
    app, problem = U.parse_manifest(_payload())
    assert problem == ""
    assert app is not None
    assert app.version == "1.9"
    assert app.url == "https://example.com/transcript-lens_v1.9.exe"
    assert app.size == 234567890
    assert app.sha256 == "a1" * 32          # 大写要归一化成小写
    assert app.notes == "这一版修了解析器，加了模型中心。"
    assert app.published == "2026-10-08"
    assert app.usable is True


def test_parse_manifest_只有顶层version也认() -> None:
    """版本号写在顶层（"这份清单描述哪个版本"）同样接受 —— 仓库里那份模板就是这写法。"""
    data = _payload()
    data["app"].pop("version", None)
    data["version"] = "1.9"
    app, problem = U.parse_manifest(data)
    assert problem == ""
    assert app is not None and app.version == "1.9"


def test_parse_manifest_app里的version优先() -> None:
    data = _payload(app={"version": "1.9"})
    data["version"] = "1.7"
    app, _ = U.parse_manifest(data)
    assert app is not None and app.version == "1.9"


def test_parse_manifest_不认识的schema当读不懂() -> None:
    app, problem = U.parse_manifest(_payload(schema=2))
    assert app is None
    assert "只认" in problem and "1" in problem


def test_parse_manifest_没有schema也认() -> None:
    data = _payload()
    del data["schema"]
    app, problem = U.parse_manifest(data)
    assert app is not None and problem == ""


@pytest.mark.parametrize(
    "payload",
    [None, 42, "manifest", [1, 2, 3], True],
    ids=["None", "int", "str", "list", "bool"],
)
def test_parse_manifest_顶层不是对象(payload: Any) -> None:
    app, problem = U.parse_manifest(payload)
    assert app is None
    assert "JSON 对象" in problem


def test_parse_manifest_没有app段() -> None:
    app, problem = U.parse_manifest({"schema": 1, "version": "1.9"})
    assert app is None
    assert "app" in problem


def test_parse_manifest_app不是对象() -> None:
    app, problem = U.parse_manifest({"schema": 1, "app": "1.9"})
    assert app is None
    assert "app" in problem


@pytest.mark.parametrize("version", ["", None, "最新版", "v", "abc"])
def test_parse_manifest_版本号读不出来(version: Any) -> None:
    app, problem = U.parse_manifest({"schema": 1, "app": {"version": version}})
    assert app is None
    assert "版本号" in problem


def test_parse_manifest_缺url就不算可用() -> None:
    app, problem = U.parse_manifest(_payload(app={"url": ""}))
    assert app is not None
    assert app.usable is False
    assert "没写全" in problem


def test_parse_manifest_缺size就不算可用() -> None:
    app, problem = U.parse_manifest(_payload(app={"size": 0}))
    assert app is not None
    assert app.usable is False
    assert problem != ""


def test_parse_manifest_缺sha256就不算可用() -> None:
    app, problem = U.parse_manifest(_payload(app={"sha256": ""}))
    assert app is not None
    assert app.usable is False
    assert problem != ""


def test_parse_manifest_sha256太短等于没写() -> None:
    app, _ = U.parse_manifest(_payload(app={"sha256": "abc123"}))
    assert app is not None
    assert app.sha256 == ""
    assert app.usable is False


def test_parse_manifest_size是布尔值要被挡掉() -> None:
    """``True`` 在 Python 里也是 int —— 不挡的话 1 字节的"体积"会被当成合法。"""
    app, _ = U.parse_manifest(_payload(app={"size": True}))
    assert app is not None
    assert app.size == 0
    assert app.usable is False


def test_parse_manifest_size是负数是零() -> None:
    app, _ = U.parse_manifest(_payload(app={"size": -5}))
    assert app is not None and app.size == 0


def test_parse_manifest_size是字符串也认() -> None:
    app, _ = U.parse_manifest(_payload(app={"size": "234567890"}))
    assert app is not None and app.size == 234567890 and app.usable is True


def test_parse_manifest_size是浮点取整() -> None:
    app, _ = U.parse_manifest(_payload(app={"size": 1024.0}))
    assert app is not None and app.size == 1024


def test_parse_manifest_size是乱码算零() -> None:
    for bad in ("", "很大", None, [], {}):
        app, _ = U.parse_manifest(_payload(app={"size": bad}))
        assert app is not None and app.size == 0


def test_parse_manifest_notes可以写顶层() -> None:
    data = _payload()
    del data["app"]["notes"]
    data["notes"] = "顶层说明"
    app, _ = U.parse_manifest(data)
    assert app is not None and app.notes == "顶层说明"


def test_parse_manifest_多出来的键不影响解析() -> None:
    data = _payload()
    data["_doc"] = ["给发版的人看的说明"]
    data["app"]["aiModels"] = ["1.5B"]
    app, problem = U.parse_manifest(data)
    assert app is not None and app.usable is True and problem == ""


def test_parse_manifest_size_text是人话() -> None:
    app, _ = U.parse_manifest(_payload())
    assert app is not None
    assert app.size_text == "223.7 MB"
    assert U.ManifestApp().size_text == ""


@pytest.mark.parametrize(
    "size,expected",
    [
        (0, ""),
        (-5, ""),
        (None, ""),
        ("abc", ""),
        (512, "512 B"),
        (2048, "2.0 KB"),
        (5 * 1024**2, "5.0 MB"),
        (2 * 1024**3, "2.00 GB"),
    ],
)
def test_human_size(size: Any, expected: str) -> None:
    assert U.human_size(size) == expected


# ---------------------------------------------------------------------------
# fetch_manifest
# ---------------------------------------------------------------------------


def test_fetch_manifest_读到好清单() -> None:
    with serve({"/update/manifest.json": json_file(GOOD_PAYLOAD)}) as base:
        app, problem = U.fetch_manifest(base + "/update/manifest.json", timeout=5)
    assert problem == ""
    assert app is not None and app.usable is True and app.version == "1.9"


def test_fetch_manifest_404说的是还没发布() -> None:
    with serve({"/update/manifest.json": json_file(GOOD_PAYLOAD)}) as base:
        app, problem = U.fetch_manifest(base + "/nope.json", timeout=5)
    assert app is None
    assert "404" in problem and "不是故障" in problem


def test_fetch_manifest_500说的是HTTP状态码() -> None:
    with serve({"/update/manifest.json": (500, b"boom", "text/plain")}) as base:
        app, problem = U.fetch_manifest(base + "/update/manifest.json", timeout=5)
    assert app is None
    assert "500" in problem


def test_fetch_manifest_不是JSON() -> None:
    with serve({"/update/manifest.json": (200, b"<html>404</html>", "text/html")}) as base:
        app, problem = U.fetch_manifest(base + "/update/manifest.json", timeout=5)
    assert app is None
    assert "不是合法 JSON" in problem


def test_fetch_manifest_连不上() -> None:
    app, problem = U.fetch_manifest(dead_url(), timeout=2)
    assert app is None
    assert problem != ""


def test_fetch_manifest_仓库里那份模板能解析但不可用() -> None:
    """★ 仓库里的 ``update/manifest.json`` 是**模板**：发版时人要填三件套。

    它必须能被解析（结构合法），但绝不能是"可用"的 ——
    否则一个还没填 url 的清单就能触发下载。
    """
    raw = (ROOT / "update" / "manifest.json").read_bytes()
    app, problem = U.parse_manifest(json.loads(raw.decode("utf-8")))
    assert app is not None, problem
    assert app.usable is False
    assert app.url == "" and app.size == 0 and app.sha256 == ""


# ---------------------------------------------------------------------------
# check_for_update：清单优先、正文兜底
# ---------------------------------------------------------------------------


class _Net:
    """假的 ``_get_json``：按 URL 查表，查不到就当"连不上"。"""

    def __init__(self, table: dict[str, tuple[int | None, Any]]) -> None:
        self.table = table
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float) -> tuple[int | None, Any]:
        self.calls.append(url)
        return self.table.get(url, (None, None))


@contextlib.contextmanager
def fake_net(monkeypatch: pytest.MonkeyPatch, table: dict[str, tuple[int | None, Any]]):
    net = _Net(table)
    monkeypatch.setattr(U, "_get_json", net)
    yield net


MANIFEST_URL = "https://example.com/update/manifest.json"


def test_清单比当前新就走清单(monkeypatch: pytest.MonkeyPatch) -> None:
    with fake_net(monkeypatch, {MANIFEST_URL: (200, GOOD_PAYLOAD)}) as net:
        check = U.check_for_update(5, current="1.8", manifest_url=MANIFEST_URL)
    assert check.status == U.STATUS_NEWER
    assert check.version == "1.9"
    assert check.source == U.SOURCE_MANIFEST
    assert check.url == GOOD_PAYLOAD["app"]["url"]
    assert check.size == 234567890
    assert check.sha256 == "a1" * 32
    assert check.notes == GOOD_PAYLOAD["app"]["notes"]
    assert check.has_direct_link is True
    assert check.size_text == "223.7 MB"
    assert "v1.9" in check.detail
    # ★ 清单能用时**一次 Gitee 请求都不该发**
    assert net.calls == [MANIFEST_URL]


def test_清单和当前一样就是已是最新(monkeypatch: pytest.MonkeyPatch) -> None:
    with fake_net(monkeypatch, {MANIFEST_URL: (200, GOOD_PAYLOAD)}):
        check = U.check_for_update(5, current="1.9", manifest_url=MANIFEST_URL)
    assert check.status == U.STATUS_CURRENT
    assert check.source == U.SOURCE_MANIFEST
    assert check.has_newer is False
    assert "已是最新" in check.detail


def test_清单比当前旧也说清楚(monkeypatch: pytest.MonkeyPatch) -> None:
    with fake_net(monkeypatch, {MANIFEST_URL: (200, GOOD_PAYLOAD)}):
        check = U.check_for_update(5, current="2.0", manifest_url=MANIFEST_URL)
    assert check.status == U.STATUS_CURRENT
    assert check.source == U.SOURCE_MANIFEST
    assert "比本机旧" in check.detail


def test_清单缺三件套就退回发行版正文(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 这条是安全属性：清单不完整时**必须**走老路子，不能凑合着下载。"""
    bad = _payload(app={"sha256": ""})
    release = {"tag_name": "v1.9", "body": "主程序\nhttps://example.com/x.exe"}
    with fake_net(
        monkeypatch,
        {
            MANIFEST_URL: (200, bad),
            U.GITEE_API: (200, release),
            U.GITEE_RELEASES_API: (200, [release]),
        },
    ) as net:
        check = U.check_for_update(5, current="1.8", manifest_url=MANIFEST_URL)
    assert check.status == U.STATUS_NEWER
    assert check.source == U.SOURCE_RELEASE
    assert check.has_direct_link is False       # 老路子不提供结构化直链
    assert "https://example.com/x.exe" in check.body
    assert net.calls[0] == MANIFEST_URL          # 先问清单
    assert U.GITEE_API in net.calls              # 再退回 Gitee


def test_清单404退回发行版正文(monkeypatch: pytest.MonkeyPatch) -> None:
    release = {"tag_name": "v1.9", "body": ""}
    with fake_net(
        monkeypatch,
        {
            MANIFEST_URL: (404, None),
            U.GITEE_API: (200, release),
            U.GITEE_RELEASES_API: (200, [release]),
        },
    ):
        check = U.check_for_update(5, current="1.8", manifest_url=MANIFEST_URL)
    assert check.status == U.STATUS_NEWER and check.source == U.SOURCE_RELEASE


def test_清单读不懂退回发行版正文(monkeypatch: pytest.MonkeyPatch) -> None:
    with fake_net(
        monkeypatch,
        {
            MANIFEST_URL: (200, "这不是 JSON 对象"),
            U.GITEE_API: (200, {"tag_name": "v1.9"}),
            U.GITEE_RELEASES_API: (200, [{"tag_name": "v1.9"}]),
        },
    ):
        check = U.check_for_update(5, current="1.8", manifest_url=MANIFEST_URL)
    assert check.status == U.STATUS_NEWER and check.source == U.SOURCE_RELEASE


def test_清单和Gitee都连不上就是检查失败(monkeypatch: pytest.MonkeyPatch) -> None:
    with fake_net(monkeypatch, {}) as net:
        check = U.check_for_update(5, current="1.8", manifest_url=MANIFEST_URL)
    assert check.status == U.STATUS_ERROR
    assert check.reachable is False
    assert check.source == U.SOURCE_RELEASE
    # 先问清单，再退回 Gitee 的"最新发行版"+"发行版列表"（老路子就是这两步）
    assert net.calls[0] == MANIFEST_URL
    assert U.GITEE_API in net.calls and U.GITEE_RELEASES_API in net.calls


def test_清单不通而仓库还没发版说的是没有发行版(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with fake_net(monkeypatch, {U.GITEE_RELEASES_API: (200, [])}):
        check = U.check_for_update(5, current="1.8", manifest_url=MANIFEST_URL)
    assert check.status == U.NO_RELEASE
    assert check.source == U.SOURCE_RELEASE
    assert "发行版" in check.detail


def test_仓库里有发行版却读不出版本号算失败(monkeypatch: pytest.MonkeyPatch) -> None:
    with fake_net(monkeypatch, {U.GITEE_RELEASES_API: (200, [{"tag_name": ""}])}):
        check = U.check_for_update(5, current="1.8", manifest_url=MANIFEST_URL)
    assert check.status == U.STATUS_ERROR
    assert "读不出" in check.detail


@pytest.mark.parametrize(
    "patch",
    [
        {"url": ""},
        {"size": 0},
        {"sha256": ""},
        {"url": "", "size": 0, "sha256": ""},
    ],
    ids=["缺url", "缺size", "缺哈希", "全缺"],
)
def test_has_direct_link三样缺一不可(patch: dict[str, Any]) -> None:
    data = _payload(app=patch)
    app, _ = U.parse_manifest(data)
    assert app is not None and app.usable is False
    check = U.UpdateCheck(status=U.STATUS_NEWER, version="1.9", **{
        "url": app.url, "size": app.size, "sha256": app.sha256
    })
    assert check.has_direct_link is False


def test_update_check默认字段不炸() -> None:
    check = U.UpdateCheck(status=U.STATUS_CURRENT)
    assert check.url == "" and check.size == 0 and check.sha256 == ""
    assert check.source == ""
    assert check.size_text == ""
    assert check.has_direct_link is False
    assert check.reachable is True and check.has_newer is False


def test_导出的名字都在() -> None:
    for name in (
        "GITEE_RAW_MANIFEST",
        "MANIFEST_SCHEMA",
        "SOURCE_MANIFEST",
        "SOURCE_RELEASE",
        "ManifestApp",
        "fetch_manifest",
        "human_size",
        "parse_manifest",
    ):
        assert name in U.__all__, name
        assert hasattr(U, name), name
    assert U.GITEE_RAW_MANIFEST.endswith("update/manifest.json")
