"""从本机制度库检索带出处的文本段落。"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import date
from pathlib import Path

from core.user_store import ROSTER_WRITE_LOCK, load_user_json, save_user_json
from core.workbench_models import installed, model_directory, verify_model

FILE = "knowledge/registry.json.enc"
MODEL_ID = "bge-small-zh-v1.5/int8-75c43b0"
MISS = "制度库中没有找到明确依据。"
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


class Embedder:
    def __init__(self, root: Path):
        from onnxruntime import InferenceSession, SessionOptions
        from tokenizers import Tokenizer

        folder = model_directory(root)
        tokenizer = verify_model(folder / "tokenizer.json", "tokenizer.json")
        self.tokenizer = Tokenizer.from_str(tokenizer.decode("utf-8"))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding()
        options = SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = InferenceSession(verify_model(folder / "bge.onnx", "bge.onnx"),
                                        sess_options=options, providers=["CPUExecutionProvider"])

    def encode(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        import numpy as np

        vectors = []
        names = {entry.name for entry in self.session.get_inputs()}
        for start in range(0, len(texts), 8):
            batch = self.tokenizer.encode_batch([
                (QUERY_PREFIX if query else "") + t for t in texts[start:start + 8]
            ])
            features = {
                "input_ids": np.asarray([e.ids for e in batch], dtype=np.int64),
                "attention_mask": np.asarray([e.attention_mask for e in batch], dtype=np.int64),
                "token_type_ids": np.asarray([e.type_ids for e in batch], dtype=np.int64),
            }
            outputs = self.session.run(None, {name: features[name] for name in names})[0]
            pooled = outputs[:, 0] if outputs.ndim == 3 else outputs
            vectors.extend((pooled / np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9)).tolist())
        return vectors


def _body(path: Path) -> list[tuple[str, str]]:
    extension = path.suffix.lower()
    if extension == ".pdf":
        from pypdf import PdfReader

        with path.open("rb") as stream:
            reader = PdfReader(stream)
            if len(reader.pages) > 500:
                raise ValueError("PDF 超过 500 页，请拆分后导入。")
            return [(f"第 {n} 页", page.extract_text() or "") for n, page in enumerate(reader.pages, 1)]
    if extension == ".docx":
        from docx import Document

        doc = Document(path)
        sections = []
        for i, item in enumerate(doc.iter_inner_content(), 1):
            body = getattr(item, "text", None)
            if body is None:
                body = "\n".join(" / ".join(cell.text for cell in row.cells) for row in item.rows)
            sections.append((f"第 {i} 个段落或表格", body))
        return sections
    if extension not in {".txt", ".md"}:
        raise ValueError("支持文本型 PDF、DOCX、TXT 和 Markdown。")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gb18030")
    return [(f"第 {n} 段", value) for n, value in enumerate(re.split(r"\n\s*\n", text), 1)]


def _chunks(content: list[tuple[str, str]]) -> list[dict]:
    output = []
    for location, text in content:
        text = re.sub(r"\s+", " ", text).strip()
        # 保留上下文重叠，真实 token 上限由 tokenizer 执行。
        for index in range(0, len(text), 280):
            output.append({"location": location, "text": text[index:index + 320]})
            if index + 320 >= len(text):
                break
    return output


def keywords(text: str) -> Counter:
    terms = []
    for token in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text.lower()):
        if token[0].isascii() or len(token) == 1:
            terms.append(token)
        else:
            terms.extend(token[pos:pos + 2] for pos in range(len(token) - 1))
    return Counter(terms)


def keyword_score(terms: Counter, value: str) -> float:
    if not terms:
        return 0.0
    content = keywords(value)
    return sum(min(freq, content.get(term, 0)) for term, freq in terms.items()) / sum(terms.values())


class KnowledgeLibrary:
    def __init__(self, store, username: str):
        self.store = store
        self.username = username

    def _data(self) -> dict:
        return load_user_json(self.store, self.username, FILE, {"documents": {}, "scope": {}}, strict=True)

    def list(self) -> list[dict]:
        return [{**{key: value for key, value in doc.items() if key != "chunks"},
                 "count": len(doc["chunks"])}
                for doc in self._data()["documents"].values()]

    def scope(self) -> dict:
        return dict(self._data().get("scope", {}))

    def set_scope(self, school: str, year: str) -> None:
        with ROSTER_WRITE_LOCK:
            data = self._data()
            data["scope"] = {"school": school.strip(), "year": year.strip()}
            save_user_json(self.store, self.username, FILE, data)

    def add(self, path: Path, metadata: dict) -> str:
        if path.stat().st_size > 30_000_000:
            raise ValueError("文件超过 30 MB，请拆分导入。")
        blob = path.read_bytes()
        digest = hashlib.sha256(blob).hexdigest()
        if not all(metadata.get(field, "").strip() for field in ("title", "source", "school", "year")):
            raise ValueError("请填写标题、来源、适用学校和学年（或通用）。")
        start = date.fromisoformat(metadata["start"]) if metadata.get("start") else None
        end = date.fromisoformat(metadata["end"]) if metadata.get("end") else None
        if start and end and start > end:
            raise ValueError("失效日期不能早于生效日期。")
        if digest in self._data()["documents"]:
            raise ValueError("这份文件已导入。")
        body = _body(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("文件在读取时发生变动，请重新选择。")
        if sum(len(page) for _, page in body) > 250_000:
            raise ValueError("文档超过 25 万字符，请拆分后导入。")
        chunks = _chunks(body)
        if not chunks:
            raise ValueError("文件没有可提取文字。扫描件请先转换成可选中文字的文件。")
        model = ""
        if installed(self.store.root, "semantic"):
            vectors = Embedder(self.store.root).encode([chunk["text"] for chunk in chunks])
            for chunk, vector in zip(chunks, vectors):
                chunk["vector"] = vector
            model = MODEL_ID
        with ROSTER_WRITE_LOCK:
            data = self._data()
            if digest in data["documents"]:
                raise ValueError("这份文件已导入。")
            data["documents"][digest] = {
                **metadata, "id": digest, "model": model, "chunks": chunks, "filename": path.name,
                "type": path.suffix.lower(), "size": len(blob), "indexed": date.today().isoformat(),
            }
            if not data["scope"]:
                data["scope"] = {name: metadata[name] for name in ("school", "year")}
            save_user_json(self.store, self.username, FILE, data)
        return digest

    def delete(self, digest: str) -> None:
        with ROSTER_WRITE_LOCK:
            data = self._data()
            data["documents"].pop(digest, None)
            save_user_json(self.store, self.username, FILE, data)

    def excerpts(self, digest: str) -> list:
        return self._data()["documents"][digest]["chunks"]

    def rebuild(self) -> int:
        model = Embedder(self.store.root)
        data = self._data()
        pending = {}
        for digest, doc in data["documents"].items():
            pending[digest] = model.encode([entry["text"] for entry in doc["chunks"]])
        with ROSTER_WRITE_LOCK:
            current = self._data()
            for digest, vectors in pending.items():
                doc = current["documents"].get(digest)
                if doc is None:
                    continue
                doc["model"] = MODEL_ID
                for entry, vector in zip(doc["chunks"], vectors):
                    entry["vector"] = vector
            save_user_json(self.store, self.username, FILE, current)
        return len(pending)

    def search(self, query: str, limit: int = 6) -> list[dict]:
        import numpy as np

        data = self._data()
        scope = data["scope"]
        school, year = scope.get("school", ""), scope.get("year", "")
        today = date.today().isoformat()
        versions = {}
        for doc in data["documents"].values():
            if doc["school"] not in (school, "通用") or doc["year"] not in (year, "通用"):
                continue
            if doc.get("start", "") > today or (doc.get("end") and doc["end"] < today):
                continue
            key = (doc["title"], doc["source"])
            value = (doc["school"] == school, doc["year"] == year, doc.get("start", ""),
                     doc.get("indexed", ""))
            if key not in versions or value >= versions[key][0]:
                versions[key] = (value, doc)
        entries = [doc for _, doc in versions.values()]
        vector = None
        if entries and any(doc.get("model") == MODEL_ID for doc in entries) and installed(self.store.root, "semantic"):
            vector = Embedder(self.store.root).encode([query], query=True)[0]
        terms = keywords(query)
        matches = []
        for doc in entries:
            for chunk in doc["chunks"]:
                lexical = keyword_score(terms, chunk["text"])
                cosine = (float(np.dot(vector, chunk["vector"])) if vector is not None
                          and doc.get("model") == MODEL_ID and chunk.get("vector") else 0.0)
                if lexical < .18 and cosine < .62:
                    continue
                matches.append({"score": lexical + .7 * max(0, cosine), "text": chunk["text"],
                                "source": f"《{doc['title']}》{chunk['location']}（{doc['source']}）"})
        return sorted(matches, key=lambda item: -item["score"])[:limit]


def policy_context(matches: list[dict]) -> str:
    if not matches:
        return MISS
    return "\n".join(f"[{number}] {match['source']}\n{match['text']}"
                      for number, match in enumerate(matches, 1))
