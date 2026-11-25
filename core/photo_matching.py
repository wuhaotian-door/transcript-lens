"""由导师核对的本地照片候选。授权、校准和向量使用都绑定当前账户。"""

import base64
import math
from datetime import date
from pathlib import Path

from core.crypto_store import CryptoError
from core.database import StudentStore
from core.knowledge import Embedder, MODEL_ID, keyword_score, keywords
from core.sensitive_vault import SensitiveVault, sensitive_filename
from core.student_import import encode_photo
from core.user_store import ROSTER_WRITE_LOCK
from core.workbench_models import installed, model_directory, verify_model

FACE_MODEL_ID = "yunet-2023mar/sface-2021dec-int8"


class FaceEngine:
    def __init__(self, root: Path):
        import cv2
        import numpy as np

        folder = model_directory(root)
        detector = np.frombuffer(verify_model(folder / "yunet.onnx", "yunet.onnx"), np.uint8)
        recognizer = np.frombuffer(verify_model(folder / "sface.onnx", "sface.onnx"), np.uint8)
        config = np.empty(0, np.uint8)
        self.cv2 = cv2
        self.detector = cv2.FaceDetectorYN.create("onnx", detector, config, (320, 320), .9)
        self.recognizer = cv2.FaceRecognizerSF.create("onnx", recognizer, config)

    def encode(self, jpeg: str) -> list[float]:
        import numpy as np

        image = self.cv2.imdecode(np.frombuffer(base64.b64decode(jpeg), np.uint8), self.cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("照片无法解码。")
        self.detector.setInputSize((image.shape[1], image.shape[0]))
        _, faces = self.detector.detect(image)
        if faces is None or len(faces) != 1:
            raise ValueError("需要一张单人正面照，当前未检测到单个人脸。")
        face = faces[0]
        if min(face[2:4]) < 80:
            raise ValueError("人脸区域太小，请换一张更清晰的照片。")
        aligned = self.recognizer.alignCrop(image, face)
        sharpness = self.cv2.Laplacian(self.cv2.cvtColor(aligned, self.cv2.COLOR_BGR2GRAY), self.cv2.CV_64F).var()
        if sharpness < 35:
            raise ValueError("照片过于模糊，请换一张照片。")
        vector = self.recognizer.feature(aligned).reshape(-1)
        norm = np.linalg.norm(vector)
        if len(vector) != 128 or not np.isfinite(norm) or norm == 0:
            raise ValueError("未提取到有效特征。")
        return (vector / norm).tolist()


def valid_consent(consent: dict) -> bool:
    return (bool(consent.get("reference")) and consent.get("expires", "") >= date.today().isoformat())


def configure_matching(vault: SensitiveVault, reference: str, threshold: float, margin: float) -> None:
    if not reference.strip() or not .0 < threshold <= 1 or not .0 < margin <= 1:
        raise ValueError("请填写校准及授权记录编号、候选阈值和最小分差。")
    vault.save_options({"enabled": True, "reference": reference.strip(),
                        "model": FACE_MODEL_ID, "threshold": threshold, "margin": margin})


def _matching_options(vault: SensitiveVault) -> dict:
    options = vault.options()
    if not options.get("enabled") or options.get("model") != FACE_MODEL_ID:
        raise CryptoError("照片匹配尚未启用。请先确认学校部署授权和此模型的校准记录。")
    return options


def set_consent(vault: SensitiveVault, sid: str, reference: str, expires: str) -> None:
    _matching_options(vault)
    if not reference.strip() or date.fromisoformat(expires) < date.today():
        raise ValueError("请记录学生单独授权编号和有效期。")
    def update(record):
        record["face_consent"] = {"reference": reference.strip(), "expires": expires}
        record.pop("face_vector", None)
    vault.update(sid, update)
    vault.audit("face_consent", sid)


def revoke_consent(vault: SensitiveVault, sid: str) -> None:
    from core.workbench_cleanup import account_directories
    from core.crypto_store import _atomic_write_bytes

    def remove(record):
        record.pop("face_consent", None)
        record.pop("face_vector", None)

    with ROSTER_WRITE_LOCK:
        vault.update(sid, remove)
        uid = StudentStore(store=vault.store, username=vault.username).uid
        filename = sensitive_filename(sid)
        for base in account_directories(vault.store, uid)[1:]:
            path = base / filename
            if path.is_file():
                data = vault._read(filename, base)
                remove(data)
                _atomic_write_bytes(path, vault._encode(filename, data))
    vault.audit("face_consent_revoked", sid)


def index_photo(vault: SensitiveVault, sid: str, engine: FaceEngine | None = None) -> None:
    _matching_options(vault)
    data = vault.student(sid)
    if not valid_consent(data.get("face_consent", {})):
        raise CryptoError("该学生没有有效的单独授权。")
    photo = data.get("photo")
    if not photo:
        raise ValueError("请先保存学生照片。")
    vector = (engine or FaceEngine(vault.store.root)).encode(photo)

    def update(record):
        if record.get("photo") != photo or not valid_consent(record.get("face_consent", {})):
            raise CryptoError("照片或授权已更新，请重试。")
        record["face_vector"] = {"model": FACE_MODEL_ID, "values": vector}

    vault.update(sid, update)
    vault.audit("face_index", sid)


def match_photo(vault: SensitiveVault, photo: Path) -> tuple[str, list[tuple[dict, float]]]:
    options = _matching_options(vault)
    query = FaceEngine(vault.store.root).encode(encode_photo(photo))
    rows = StudentStore(store=vault.store, username=vault.username).rows(include_archived=False)
    candidates = []
    with ROSTER_WRITE_LOCK:
        for row in rows:
            data = vault.student(row["student_id"])
            vector = data.get("face_vector", {})
            values = vector.get("values", ())
            if not valid_consent(data.get("face_consent", {})) or vector.get("model") != FACE_MODEL_ID:
                continue
            score = sum(left * right for left, right in zip(query, values))
            if len(values) == 128 and math.isfinite(score):
                candidates.append((row, score))
    candidates.sort(key=lambda item: -item[1])
    vault.audit("face_match")
    if not candidates or candidates[0][1] < options["threshold"]:
        return "未找到可靠候选，请按学号核对。", []
    if len(candidates) > 1 and candidates[0][1] - candidates[1][1] < options["margin"]:
        return "候选分差较小，请核对原始照片。", candidates[:3]
    return "候选需人工确认。", candidates[:3]


def interview_vector(root: Path, text: str) -> dict:
    if not installed(root, "semantic"):
        return {}
    return {"model": MODEL_ID, "values": Embedder(root).encode([text])[0]}


def search_interviews(vault: SensitiveVault, sid: str, query: str) -> list[dict]:
    from datetime import datetime

    import numpy as np

    records = vault.student(sid).get("interviews", [])
    terms = keywords(query)
    vector = None
    if installed(vault.store.root, "semantic") and any(
        row.get("vector", {}).get("model") == MODEL_ID for row in records
    ):
        vector = Embedder(vault.store.root).encode([query], query=True)[0]
    scored = []
    for entry in records:
        lexical = keyword_score(terms, entry["content"])
        saved = entry.get("vector", {})
        similarity = float(np.dot(vector, saved["values"])) if vector is not None and saved.get("model") == MODEL_ID else 0.0
        if lexical <= .15 and similarity <= .6:
            continue
        age = max(0, (date.today() - datetime.fromisoformat(entry["date"]).date()).days)
        scored.append((lexical + .7 * similarity + .1 / (1 + age / 180), entry))
    return [entry for _, entry in sorted(scored, key=lambda item: -item[0])[:5]]
