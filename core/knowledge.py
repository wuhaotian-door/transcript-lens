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


FIELD_TITLES = {"title": "标题", "source": "发布部门或来源", "school": "适用学校", "year": "学年", "version": "版本"}
FIELD_LENGTHS = {"title": 120, "source": 240, "school": 100, "year": 12, "version": 60}


class DocumentValidationError(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


class DuplicateDocumentError(ValueError):
    def __init__(self, digest: str, title: str):
        super().__init__("这份文件已导入。")
        self.digest = digest
        self.title = title


def _text_field(key: str, value, *, optional: bool = False) -> str:
    text = value.strip() if isinstance(value, str) else ""
    field = FIELD_TITLES[key]
    if not text and not optional:
        raise DocumentValidationError(key, f"请填写{field}。")
    limit = FIELD_LENGTHS[key]
    if len(text) > limit or any(not character.isprintable() for character in text):
        raise DocumentValidationError(key, f"{field}请使用单行文字，最多 {limit} 字。")
    return text


def validate_scope(school: str, year: str) -> dict[str, str]:
    school = _text_field("school", school)
    year = _text_field("year", year).removesuffix("学年").strip()
    year = year.replace("—", "-").replace("–", "-")
    if year != "通用":
        years = re.fullmatch(r"([0-9]{4})-([0-9]{4})", year)
        if not years or not 1900 <= int(years[1]) < 9999 or int(years[2]) != int(years[1]) + 1:
            raise DocumentValidationError("year", "学年请选择连续的两个年份，例如 2025-2026，或选择「通用」。")
    return {"school": school, "year": year}


def validate_metadata(metadata: dict) -> dict[str, str]:
    fields = {key: _text_field(key, metadata.get(key)) for key in ("title", "source")}
    fields.update(validate_scope(metadata.get("school"), metadata.get("year")))
    fields["version"] = _text_field("version", metadata.get("version"), optional=True)
    for key, title in (("start", "生效日期"), ("end", "截止日期")):
        value = metadata.get(key) or ""
        if key == "end" and value == "至今":
            value = ""
        try:
            if value:
                if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                    raise ValueError
                value = date.fromisoformat(value).isoformat()
        except ValueError as error:
            raise DocumentValidationError(key, f"请选择有效的{title}。") from error
        fields[key] = value
    if fields["start"] and fields["end"] and fields["start"] > fields["end"]:
        raise DocumentValidationError("end", "截止日期不能早于生效日期。")
    return fields


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

    def list(self, query: str = "") -> list[dict]:
        """管理资料时检索所有版本，也包含尚未生效或已过期的文档。"""
        terms = query.casefold().split()
        result = []
        for doc in self._data()["documents"].values():
            metadata = {key: value for key, value in doc.items() if key != "chunks"}
            chunks = doc["chunks"]
            if terms:
                searchable = " ".join(str(doc.get(key) or "") for key in (
                    *FIELD_TITLES, "filename", "start", "end"))
                searchable += " " + " ".join(chunk["text"] for chunk in chunks)
                searchable = searchable.casefold()
                if not all(term in searchable for term in terms):
                    continue
            result.append({**metadata, "count": len(chunks)})
        return result

    def edit(self, digest: str, metadata: dict) -> None:
        cleaned = validate_metadata(metadata)
        with ROSTER_WRITE_LOCK:
            data = self._data()
            doc = data["documents"].get(digest)
            if doc is None:
                raise ValueError("这份资料已从知识库移除。")
            doc.update(cleaned)
            save_user_json(self.store, self.username, FILE, data)

    def scope(self) -> dict:
        return dict(self._data().get("scope", {}))

    def set_scope(self, school: str, year: str) -> None:
        scope = validate_scope(school, year)
        with ROSTER_WRITE_LOCK:
            data = self._data()
            data["scope"] = scope
            save_user_json(self.store, self.username, FILE, data)

    def find_duplicate(self, path: Path) -> dict | None:
        if path.stat().st_size > 30_000_000:
            raise ValueError("文件超过 30 MB，请拆分导入。")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        doc = self._data()["documents"].get(digest)
        return {key: value for key, value in doc.items() if key != "chunks"} if doc else None

    def add(self, path: Path, metadata: dict, *, overwrite_digest: str | None = None) -> str:
        metadata = validate_metadata(metadata)
        if path.stat().st_size > 30_000_000:
            raise ValueError("文件超过 30 MB，请拆分导入。")
        blob = path.read_bytes()
        if len(blob) > 30_000_000:
            raise ValueError("文件超过 30 MB，请拆分导入。")
        digest = hashlib.sha256(blob).hexdigest()
        previous = self._data()["documents"].get(digest)
        if overwrite_digest is not None and digest != overwrite_digest:
            raise ValueError("文件在确认后已变化，请重新选择。")
        if previous is not None and overwrite_digest is None:
            raise DuplicateDocumentError(digest, previous["title"])
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
            existing = data["documents"].get(digest)
            if existing is not None and overwrite_digest is None:
                raise DuplicateDocumentError(digest, existing["title"])
            if existing is None and overwrite_digest is not None:
                raise ValueError("原资料已被移除，请重新导入。")
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
