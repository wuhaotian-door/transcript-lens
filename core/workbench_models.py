"""本地模型的固定来源和完整性检查。"""

import hashlib
from pathlib import Path

from core.crypto_store import _atomic_write_bytes
from core.updater import download_exe

FACE_URL = "https://huggingface.co/opencv/opencv_zoo/resolve/d4938dfc9d4ec5d098bfa33e98b3f3345a236586/"
TEXT_URL = "https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/75c43b069aac4d136ba6bc1122f995fedcfd2781/"
ASSETS = {
    "yunet.onnx": (FACE_URL + "models/face_detection_yunet/face_detection_yunet_2023mar.onnx", 232589,
                   "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    "sface.onnx": (FACE_URL + "models/face_recognition_sface/face_recognition_sface_2021dec_int8.onnx", 9896933,
                   "2b0e941e6f16cc048c20aee0c8e31f569118f65d702914540f7bfdc14048d78a"),
    "bge.onnx": (TEXT_URL + "onnx/model_quantized.onnx", 24010842,
                 "15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc"),
    "tokenizer.json": (TEXT_URL + "tokenizer.json", 439125,
                       "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26"),
}
GROUPS = {"semantic": ("bge.onnx", "tokenizer.json"), "face": ("yunet.onnx", "sface.onnx")}


def model_directory(root: Path) -> Path:
    return root / "models" / "workbench"


def installed(root: Path, group: str) -> bool:
    for name in GROUPS[group]:
        path = model_directory(root) / name
        if not path.is_file() or path.stat().st_size != ASSETS[name][1]:
            return False
    return True


def verify_model(path: Path, name: str) -> bytes:
    _, size, digest = ASSETS[name]
    with path.open("rb") as stream:
        data = stream.read(size + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"{name} 模型文件校验失败。")
    return data


def install_models(root: Path, group: str, source: Path | None = None) -> None:
    dest = model_directory(root)
    names = GROUPS[group]
    if source is not None:
        checked = {name: verify_model(source / name, name) for name in names}
        for name, blob in checked.items():
            _atomic_write_bytes(dest / name, blob)
        return
    for name in names:
        url, size, digest = ASSETS[name]
        target = dest / name
        try:
            verify_model(target, name)
            continue
        except (FileNotFoundError, ValueError):
            pass
        download_exe(url, target, expected_sha256=digest, min_bytes=size, resume=False)
