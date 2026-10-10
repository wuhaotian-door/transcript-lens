# -*- coding: utf-8 -*-
"""分片下载与拼接（``core.updater.download_parts``，v1.8.1 新增）。

为什么要分片：Gitee 社区版**单个附件上限 100MB**
（官方帮助"创建 Release"页原文：单个附件不能超过 100M），而 onefile exe 是
240MB 量级 —— 传不上去，只能切几片分别传。切片把风险从"一个文件"变成
"N 个文件 + 一次拼接"，所以这一段代码的保证必须比 :func:`download_exe`
**只多不少**，而这个文件就是它的判据：

1. 逐片校验：坏片当场丢弃，绝不混进拼接；
2. 顺序固定：拼接顺序 = 清单顺序，不看文件名；
3. 整体再校验：拼完核**整个 exe** 的 sha256 与字节数；
4. 失败/取消**绝不损坏已有 exe**（全程只写 ``<dest>.part``）；
5. 可续传：已校验的片再点一次**连请求都不发**；
6. 成功后清掉 ``<dest>.parts/``。

★ 全程只打本机 ``http.server``，**不联网**。
"""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import threading
from pathlib import Path

import pytest

from core import updater as U

#: 73KB 假数据（够跨好几个 CHUNK_SIZE，又不至于让测试变慢）
ORIGINAL = bytes((i * 31 + 7) % 251 for i in range(73_000))

#: 切成三片，故意第 3 片短一截（真实发布最后一片也短）
PIECE_SIZES = (30_000, 30_000, 13_000)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pieces_of(data: bytes = ORIGINAL) -> list[bytes]:
    out: list[bytes] = []
    offset = 0
    for size in PIECE_SIZES:
        out.append(data[offset : offset + size])
        offset += size
    assert b"".join(out) == data
    return out


# ---------------------------------------------------------------------------
# 本机 http.server（**支持 Range**：片级续传要能真的走 206）
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def serve(blobs: dict[str, bytes]):
    """yield ``(根地址, 命中记录)``；命中记录是 ``(path, Range 头)`` 的列表。"""
    hits: list[tuple[str, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口
            hits.append((self.path, self.headers.get("Range", "")))
            body = blobs.get(self.path)
            if body is None:
                self.send_error(404)
                return
            raw = self.headers.get("Range", "")
            start = 0
            if raw.startswith("bytes="):
                head = raw[len("bytes=") :].split("-", 1)[0]
                if head.isdigit():
                    start = int(head)
            chunk = body[start:]
            self.send_response(206 if start else 200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(chunk)))
            if start:
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}"
                )
            self.end_headers()
            self.wfile.write(chunk)

        def log_message(self, *args: object) -> None:
            return

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", hits
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def manifest_parts(base: str, blobs: list[bytes]) -> tuple[U.ManifestPart, ...]:
    return tuple(
        U.ManifestPart(url=f"{base}/part{i}.bin", size=len(blob), sha256=sha(blob))
        for i, blob in enumerate(blobs, start=1)
    )


def run(parts, dest: Path, **kwargs):
    """下载（小样本，所以 ``min_bytes=0``）。"""
    kwargs.setdefault("expected_sha256", sha(ORIGINAL))
    kwargs.setdefault("expected_size", len(ORIGINAL))
    return U.download_parts(parts, dest, kwargs.pop("progress", None),
                            min_bytes=0, **kwargs)


# ---------------------------------------------------------------------------
# 1. 正常路径
# ---------------------------------------------------------------------------


def test_分片下载拼接后与原文逐字节一致(tmp_path: Path) -> None:
    blobs = pieces_of()
    with serve({f"/part{i}.bin": b for i, b in enumerate(blobs, start=1)}) as (base, _):
        seen: list[tuple[int, int | None]] = []
        dest = tmp_path / "sub" / "transcript-lens.exe"
        got = run(
            manifest_parts(base, blobs), dest, progress=lambda d, t: seen.append((d, t))
        )
    assert got.read_bytes() == ORIGINAL
    assert seen and seen[-1] == (len(ORIGINAL), len(ORIGINAL))
    assert [d for d, _ in seen] == sorted(d for d, _ in seen), "进度必须单调不减"


def test_分片下载成功后清掉缓存目录(tmp_path: Path) -> None:
    blobs = pieces_of()
    with serve({f"/part{i}.bin": b for i, b in enumerate(blobs, start=1)}) as (base, _):
        dest = tmp_path / "a.exe"
        run(manifest_parts(base, blobs), dest)
    assert not (tmp_path / "a.exe.parts").exists(), "下完就该清掉 .parts 缓存"
    assert not (tmp_path / "a.exe.part").exists(), "临时文件也不该留下"


def test_拼接顺序按清单而不是按文件名(tmp_path: Path) -> None:
    blobs = pieces_of()
    # 把三片的 URL 故意错开：清单顺序 3-1-2，拼出来必须是 1-2-3 的原样
    files = {
        "/a.bin": blobs[0],
        "/b.bin": blobs[1],
        "/c.bin": blobs[2],
    }
    order = ["/a.bin", "/b.bin", "/c.bin"]
    with serve(files) as (base, _):
        parts = tuple(
            U.ManifestPart(
                url=f"{base}{name}",
                size=len(files[name]),
                sha256=sha(files[name]),
            )
            for name in order
        )
        dest = tmp_path / "b.exe"
        run(parts, dest)
    assert dest.read_bytes() == ORIGINAL


# ---------------------------------------------------------------------------
# 2. 坏片 / 缺片 / 体积与哈希对不上 —— 一个都不能放过
# ---------------------------------------------------------------------------


def test_坏片当场被拒绝且不动目标文件(tmp_path: Path) -> None:
    blobs = pieces_of()
    with serve({f"/part{i}.bin": b for i, b in enumerate(blobs, start=1)}) as (base, _):
        parts = list(manifest_parts(base, blobs))
        parts[1] = U.ManifestPart(url=parts[1].url, size=parts[1].size, sha256="0" * 64)
        target = tmp_path / "existing.exe"
        target.write_bytes(b"PRECIOUS")
        with pytest.raises(U.DownloadError):
            run(tuple(parts), target)
    assert target.read_bytes() == b"PRECIOUS", "校验不过时已有 exe 必须一个字节没动"


def test_缺片时直接失败(tmp_path: Path) -> None:
    blobs = pieces_of()
    with serve({"/part1.bin": blobs[0], "/part3.bin": blobs[2]}) as (base, _):
        parts = manifest_parts(base, blobs)
        with pytest.raises(U.DownloadError):
            run(parts, tmp_path / "c.exe")
    assert not (tmp_path / "c.exe").exists()


def test_整体体积对不上要拒绝(tmp_path: Path) -> None:
    blobs = pieces_of()
    with serve({f"/part{i}.bin": b for i, b in enumerate(blobs, start=1)}) as (base, _):
        dest = tmp_path / "d.exe"
        with pytest.raises(U.DownloadError) as info:
            run(manifest_parts(base, blobs), dest, expected_size=len(ORIGINAL) + 1)
    assert "字节" in str(info.value)
    assert not dest.exists()


def test_整体哈希对不上要拒绝(tmp_path: Path) -> None:
    blobs = pieces_of()
    with serve({f"/part{i}.bin": b for i, b in enumerate(blobs, start=1)}) as (base, _):
        dest = tmp_path / "e.exe"
        with pytest.raises(U.DownloadError) as info:
            run(manifest_parts(base, blobs), dest, expected_sha256="f" * 64)
    assert "SHA256" in str(info.value)
    assert not dest.exists()


def test_没有任何分片时直接报错(tmp_path: Path) -> None:
    with pytest.raises(U.DownloadError):
        U.download_parts((), tmp_path / "f.exe", min_bytes=0)


# ---------------------------------------------------------------------------
# 3. 取消与续传
# ---------------------------------------------------------------------------


def test_取消时保留分片缓存且不动目标文件(tmp_path: Path) -> None:
    blobs = pieces_of()
    with serve({f"/part{i}.bin": b for i, b in enumerate(blobs, start=1)}) as (base, _):
        target = tmp_path / "g.exe"
        target.write_bytes(b"OLD")
        calls = {"n": 0}

        def cancel() -> bool:
            calls["n"] += 1
            return calls["n"] > 3          # 下几块之后就取消

        with pytest.raises(U.DownloadCancelled):
            run(manifest_parts(base, blobs), target, cancel=cancel)
        assert target.read_bytes() == b"OLD"
        assert (tmp_path / "g.exe.parts").exists(), "缓存要留着给下次续传"


def test_已校验的片再下时不发请求(tmp_path: Path) -> None:
    blobs = pieces_of()
    dest = tmp_path / "h.exe"
    pool = tmp_path / "h.exe.parts"
    pool.mkdir()
    for i, blob in enumerate(blobs, start=1):
        (pool / f"part{i:02d}").write_bytes(blob)
    with serve({}) as (base, hits):        # 服务器上什么都**没有**
        run(manifest_parts(base, blobs), dest)
    assert hits == [], "三片都已在本地且校验通过，不该发任何请求"
    assert dest.read_bytes() == ORIGINAL


def test_片长不对的缓存要重下(tmp_path: Path) -> None:
    blobs = pieces_of()
    dest = tmp_path / "i.exe"
    pool = tmp_path / "i.exe.parts"
    pool.mkdir()
    (pool / "part01").write_bytes(blobs[0][:100])      # 半截
    with serve({f"/part{i}.bin": b for i, b in enumerate(blobs, start=1)}) as (base, hits):
        run(manifest_parts(base, blobs), dest)
    assert [path for path, _ in hits] == ["/part1.bin", "/part2.bin", "/part3.bin"], \
        "半截的那片必须重下，别把它拼进去"
    assert dest.read_bytes() == ORIGINAL


# ---------------------------------------------------------------------------
# 4. 清单侧的判据（分片体积之和 / Gitee 上限）
# ---------------------------------------------------------------------------


def _app(parts: tuple[U.ManifestPart, ...], *, size: int | None = None) -> U.ManifestApp:
    return U.ManifestApp(
        version="1.8.1",
        size=len(ORIGINAL) if size is None else size,
        sha256=sha(ORIGINAL),
        parts=parts,
    )


def test_体积之和与整体对不上时清单不可用() -> None:
    blobs = pieces_of()
    parts = tuple(
        U.ManifestPart(url=f"http://example.com/{i}", size=len(b), sha256=sha(b))
        for i, b in enumerate(blobs, start=1)
    )
    assert _app(parts).usable is True
    assert _app(parts, size=len(ORIGINAL) + 1).usable is False


def test_缺了整体哈希的分片清单不可用() -> None:
    blobs = pieces_of()
    parts = tuple(
        U.ManifestPart(url=f"http://example.com/{i}", size=len(b), sha256=sha(b))
        for i, b in enumerate(blobs, start=1)
    )
    app = U.ManifestApp(version="1.8.1", size=len(ORIGINAL), sha256="", parts=parts)
    assert app.usable is False


def test_gitee上超过100MB的片不可用() -> None:
    huge = U.ATTACHMENT_LIMIT_BYTES + 1
    part = U.ManifestPart(
        url="https://gitee.com/wuhaotiansystem/transcript-lens/releases/download/1.8.1/x.part1",
        size=huge,
        sha256="a" * 64,
    )
    assert part.oversize_on_gitee is True
    assert _app((part,), size=huge).usable is False


def test_托管在别处的片不受100MB限制() -> None:
    huge = U.ATTACHMENT_LIMIT_BYTES + 1
    part = U.ManifestPart(
        url="https://example.com/big.part1", size=huge, sha256="b" * 64
    )
    assert part.oversize_on_gitee is False
    assert _app((part,), size=huge).usable is True
