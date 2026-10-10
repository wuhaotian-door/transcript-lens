# -*- coding: utf-8 -*-
"""发版工具：把打包好的 exe 切成几片，并打印能直接贴进 ``update/manifest.json`` 的 ``app`` 段。

**为什么需要它**：Gitee 社区版**单个附件上限 100MB**
（官方帮助"创建 Release"页原文：单个附件不能超过 100M，GVP 项目 200M），
而我们的 onefile exe 是 240MB 量级 —— 传不上去。于是发版时切成几片分别当附件传，
程序端由 :func:`core.updater.download_parts` 逐片校验、按清单顺序拼回来、
再核对**整体** sha256。

用法::

    python tools/split_release.py dist\\transcript-lens.exe --parts 3 \\\\
        --tag 1.8.1 --version 1.8.1 --notes "这一版修了解析器" --out release

它做三件事：

1. 按 ``--parts`` 把文件**顺序**切成几片，落在 ``--out`` 目录里，
   文件名 ``transcript-lens-v<版本>.exe.partN``（N 从 1 开始，**没有前导零** ——
   附件的文件名要能直接拼 URL）；
2. 每一片都**先自查**：体积不许超过 :data:`core.updater.ATTACHMENT_LIMIT_BYTES`
   （超了就要求你多切几片，而不是"传上去再说"）；
3. 打印一份 JSON —— ``app`` 段（``size``/``sha256`` 是**整个 exe** 的，
   ``parts`` 是每片自己的），以及每片的 sha256 以便核对上传结果。

★ 这个脚本**不联网、不上传**：上传要么在 Gitee 网页上拖，要么用 API
（``POST /repos/{owner}/{repo}/releases/{release_id}/attach_files``）。
发完务必用 ``--verify`` 复查一遍直链能不能下、体积对不对。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core.updater import ATTACHMENT_LIMIT_BYTES  # noqa: E402

GITEE_REPO = "wuhaotiansystem/transcript-lens"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def split(source: Path, parts: int, out_dir: Path, stem: str) -> list[Path]:
    """把 ``source`` 顺序切成 ``parts`` 片，返回切好的文件列表（顺序即拼接顺序）。"""
    size = source.stat().st_size
    if parts < 1:
        raise SystemExit("--parts 至少是 1")
    chunk = -(-size // parts)                      # 向上取整
    if chunk > ATTACHMENT_LIMIT_BYTES:
        need = -(-size // ATTACHMENT_LIMIT_BYTES)
        raise SystemExit(
            f"切 {parts} 片的话每片 {chunk} 字节，超过 Gitee 单附件上限 "
            f"{ATTACHMENT_LIMIT_BYTES} 字节 —— 至少切 {need} 片。"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with open(source, "rb") as handle:
        for index in range(1, parts + 1):
            target = out_dir / f"{stem}.part{index}"
            remaining = chunk
            with open(target, "wb") as sink:
                while remaining > 0:
                    block = handle.read(min(1024 * 1024, remaining))
                    if not block:
                        break
                    sink.write(block)
                    remaining -= len(block)
            written.append(target)
            if target.stat().st_size == 0:         # 片数多于需要的块数
                target.unlink()
                break
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="把 exe 切成 Gitee 附件能收下的几片")
    parser.add_argument("exe", type=Path, help="要切的 exe（一般是 dist\\transcript-lens.exe）")
    parser.add_argument("--parts", type=int, default=3, help="切成几片（默认 3）")
    parser.add_argument("--tag", required=True, help="Gitee 标签名（**照抄真实标签**，如 1.8.1）")
    parser.add_argument("--version", required=True, help="这次发布的程序版本号，如 1.8.1")
    parser.add_argument("--notes", default="", help="发版说明（写进 app.notes）")
    parser.add_argument("--out", type=Path, default=Path("release"), help="分片输出目录")
    args = parser.parse_args()

    source: Path = args.exe
    if not source.is_file():
        raise SystemExit(f"找不到 {source}")

    stem = f"transcript-lens-v{args.version}.exe"
    files = split(source, args.parts, args.out, stem)

    total = source.stat().st_size
    parts_payload = []
    print(f"源文件 {source} = {total} 字节，sha256 {sha256_of(source)}", file=sys.stderr)
    for index, path in enumerate(files, start=1):
        size = path.stat().st_size
        digest = sha256_of(path)
        parts_payload.append(
            {
                "url": (
                    f"https://gitee.com/{GITEE_REPO}/releases/download/"
                    f"{args.tag}/{path.name}"
                ),
                "size": size,
                "sha256": digest,
            }
        )
        print(f"  第 {index} 片 {path.name}: {size} 字节  {digest}", file=sys.stderr)

    app = {
        "url": "",
        "size": total,
        "sha256": sha256_of(source),
        "notes": args.notes,
        "parts": parts_payload,
    }
    print("把下面这段作为 app 段贴进 update/manifest.json：", file=sys.stderr)
    print(json.dumps({"schema": 1, "version": args.version, "app": app},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
