from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_CHUNKS = 50_000
CHUNK_SIZE = 1_000
CHUNK_OVERLAP = 150
IDLE_CACHE_SECONDS = 600

EmbeddingClient = Callable[[dict[str, Any], list[str]], Awaitable[list[list[float]]]]
OCRClient = Callable[[dict[str, Any], bytes, str, str], Awaitable[str]]


class KnowledgeStore:
    def __init__(
        self,
        root: str | Path,
        *,
        database_url: str | None = None,
        max_file_bytes: int = MAX_FILE_BYTES,
        max_total_bytes: int = MAX_TOTAL_BYTES,
        max_chunks: int = MAX_CHUNKS,
    ) -> None:
        self.root = Path(root)
        self.files_dir = self.root / "files"
        self.uploads_dir = self.root / "uploads"
        self.vector_dir = self.root / "lancedb"
        self.db_path = self.root / "knowledge.sqlite3"
        self.database_url = (database_url or os.getenv("AGENT_KNOWLEDGE_DATABASE_URL") or "").strip()
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_chunks = max_chunks
        self._uploads: dict[str, dict[str, Any]] = {}
        self._index_lock = asyncio.Lock()
        self._last_access = time.time()
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.vector_dir.mkdir(parents=True, exist_ok=True)
        self.vector_index = LanceVectorIndex(self.vector_dir)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if self.database_url:
            return PostgresConnection(self.database_url)  # type: ignore[return-value]
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_knowledge_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_knowledge_sources (
                    source_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS agent_knowledge_sources_sha_idx
                    ON agent_knowledge_sources(sha256);
                CREATE TABLE IF NOT EXISTS agent_knowledge_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    FOREIGN KEY(source_id) REFERENCES agent_knowledge_sources(source_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS agent_knowledge_chunks_source_idx
                    ON agent_knowledge_chunks(source_id, position);
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO agent_knowledge_config (id, enabled, updated_at) VALUES (1, 0, ?)",
                (int(time.time()),),
            )

    def config_get(self) -> dict[str, Any]:
        self._touch()
        with self._connect() as conn:
            row = conn.execute("SELECT enabled, updated_at FROM agent_knowledge_config WHERE id = 1").fetchone()
        return {
            "enabled": bool(row["enabled"]) if row else False,
            "embedding_required": True,
            "ocr_optional": True,
            "limits": {
                "max_file_bytes": self.max_file_bytes,
                "max_total_bytes": self.max_total_bytes,
                "max_chunks": self.max_chunks,
                "idle_cache_seconds": IDLE_CACHE_SECONDS,
            },
        }

    def config_update(self, params: dict[str, Any]) -> dict[str, Any]:
        self._touch()
        enabled = bool(params.get("enabled"))
        if enabled and not embedding_profile_ready(params.get("embedding_profile")):
            raise ValueError("embedding_profile is required")
        with self._connect() as conn:
            conn.execute(
                "UPDATE agent_knowledge_config SET enabled = ?, updated_at = ? WHERE id = 1",
                (1 if enabled else 0, int(time.time())),
            )
        return {"ok": True, **self.config_get()}

    def sources_list(self) -> dict[str, Any]:
        self._touch()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.*, COALESCE(c.chunk_count, 0) AS chunk_count
                FROM agent_knowledge_sources s
                LEFT JOIN (
                    SELECT source_id, COUNT(*) AS chunk_count
                    FROM agent_knowledge_chunks
                    GROUP BY source_id
                ) c ON c.source_id = s.source_id
                ORDER BY s.created_at DESC
                """
            ).fetchall()
        return {"sources": [source_dict(row) for row in rows]}

    def source_delete(self, source_id: str) -> dict[str, Any]:
        self._touch()
        if not source_id:
            raise ValueError("source_id is required")
        with self._connect() as conn:
            row = conn.execute("SELECT sha256 FROM agent_knowledge_sources WHERE source_id = ?", (source_id,)).fetchone()
            deleted = conn.execute("DELETE FROM agent_knowledge_sources WHERE source_id = ?", (source_id,)).rowcount
        if row:
            file_path = self.files_dir / str(row["sha256"])
            if file_path.exists():
                file_path.unlink()
        self.vector_index.delete_source(source_id)
        return {"ok": True, "deleted": deleted > 0, "source_id": source_id}

    def upload_start(self, params: dict[str, Any]) -> dict[str, Any]:
        self._touch()
        filename = sanitize_filename(str(params.get("filename") or params.get("name") or "document.txt"))
        mime_type = str(params.get("mime_type") or params.get("content_type") or guess_mime(filename)).strip()
        size = int(params.get("size") or 0)
        if size > self.max_file_bytes:
            raise ValueError("file exceeds max_file_bytes")
        if requires_ocr(filename, mime_type) and not ocr_profile_ready(params.get("ocr_profile")):
            raise ValueError("ocr_profile is required")
        if not is_supported_document(filename, mime_type):
            raise ValueError("unsupported document type")
        self._check_capacity(size)
        upload_id = uuid.uuid4().hex
        temp_path = self.uploads_dir / upload_id
        self._uploads[upload_id] = {
            "filename": filename,
            "mime_type": mime_type,
            "size": size,
            "received": 0,
            "path": temp_path,
            "created_at": int(time.time()),
        }
        temp_path.write_bytes(b"")
        return {"upload_id": upload_id, "chunk_size": 256 * 1024, "received": 0}

    def upload_chunk(self, params: dict[str, Any]) -> dict[str, Any]:
        self._touch()
        upload = self._upload(params)
        raw = params.get("data") or params.get("chunk") or ""
        try:
            payload = base64.b64decode(str(raw), validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("chunk data must be base64") from exc
        received = int(upload["received"]) + len(payload)
        if received > self.max_file_bytes:
            raise ValueError("file exceeds max_file_bytes")
        with Path(upload["path"]).open("ab") as handle:
            handle.write(payload)
        upload["received"] = received
        return {"ok": True, "upload_id": str(params.get("upload_id") or ""), "received": received}

    async def upload_finish(
        self,
        params: dict[str, Any],
        *,
        embedding_client: EmbeddingClient | None = None,
        ocr_client: OCRClient | None = None,
    ) -> dict[str, Any]:
        self._touch()
        embedding_client = embedding_client or openai_compatible_embeddings
        ocr_client = ocr_client or openai_compatible_ocr
        upload = self._upload(params)
        embedding_profile = require_embedding_profile(params.get("embedding_profile"))
        filename = str(upload["filename"])
        mime_type = str(upload["mime_type"])
        path = Path(upload["path"])
        data = path.read_bytes()
        if int(upload.get("size") or 0) and len(data) != int(upload["size"]):
            raise ValueError("uploaded size mismatch")
        sha = hashlib.sha256(data).hexdigest()
        with self._connect() as conn:
            existing = conn.execute("SELECT source_id, status FROM agent_knowledge_sources WHERE sha256 = ?", (sha,)).fetchone()
        if existing:
            path.unlink(missing_ok=True)
            self._uploads.pop(str(params.get("upload_id") or ""), None)
            return {"ok": True, "deduplicated": True, "source": self._source_by_id(str(existing["source_id"]))}

        title = str(params.get("title") or filename).strip() or filename
        text = await extract_text(data, filename, mime_type, params.get("ocr_profile"), ocr_client)
        async with self._index_lock:
            source = await self._index_text(
                title=title,
                kind="document",
                mime_type=mime_type,
                data=data,
                sha256=sha,
                text=text,
                embedding_profile=embedding_profile,
                embedding_client=embedding_client,
            )
        path.unlink(missing_ok=True)
        self._uploads.pop(str(params.get("upload_id") or ""), None)
        return {"ok": True, "deduplicated": False, "source": source}

    async def memory_create(
        self,
        params: dict[str, Any],
        *,
        embedding_client: EmbeddingClient | None = None,
    ) -> dict[str, Any]:
        self._touch()
        embedding_client = embedding_client or openai_compatible_embeddings
        embedding_profile = require_embedding_profile(params.get("embedding_profile"))
        text = str(params.get("text") or params.get("content") or "").strip()
        if not text:
            raise ValueError("text is required")
        title = str(params.get("title") or "Memory").strip() or "Memory"
        data = text.encode("utf-8")
        sha = hashlib.sha256((title + "\n" + text).encode("utf-8")).hexdigest()
        with self._connect() as conn:
            existing = conn.execute("SELECT source_id FROM agent_knowledge_sources WHERE sha256 = ?", (sha,)).fetchone()
        if existing:
            return {"ok": True, "deduplicated": True, "source": self._source_by_id(str(existing["source_id"]))}
        async with self._index_lock:
            source = await self._index_text(
                title=title,
                kind="memory",
                mime_type="text/plain",
                data=data,
                sha256=sha,
                text=text,
                embedding_profile=embedding_profile,
                embedding_client=embedding_client,
            )
        return {"ok": True, "deduplicated": False, "source": source}

    async def search(
        self,
        params: dict[str, Any],
        *,
        embedding_client: EmbeddingClient | None = None,
    ) -> dict[str, Any]:
        self._touch()
        embedding_client = embedding_client or openai_compatible_embeddings
        embedding_profile = require_embedding_profile(params.get("embedding_profile"))
        query = str(params.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        top_k = max(1, min(int(params.get("top_k") or params.get("limit") or 5), 20))
        source_ids = params.get("source_ids")
        if not isinstance(source_ids, list):
            source_ids = []
        query_embedding = (await embedding_client(embedding_profile, [query]))[0]
        vector_results = self.vector_index.search(query_embedding, [str(item) for item in source_ids], top_k)
        if vector_results is not None:
            return {"results": vector_results}
        with self._connect() as conn:
            if source_ids:
                placeholders = ",".join("?" for _ in source_ids)
                rows = conn.execute(
                    f"""
                    SELECT c.chunk_id, c.source_id, c.position, c.text, c.embedding_json, s.title, s.kind
                    FROM agent_knowledge_chunks c
                    JOIN agent_knowledge_sources s ON s.source_id = c.source_id
                    WHERE c.source_id IN ({placeholders}) AND s.status = 'ready'
                    """,
                    tuple(str(item) for item in source_ids),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT c.chunk_id, c.source_id, c.position, c.text, c.embedding_json, s.title, s.kind
                    FROM agent_knowledge_chunks c
                    JOIN agent_knowledge_sources s ON s.source_id = c.source_id
                    WHERE s.status = 'ready'
                    """
                ).fetchall()
        scored: list[dict[str, Any]] = []
        for row in rows:
            try:
                embedding = [float(item) for item in json.loads(str(row["embedding_json"]))]
            except Exception:  # noqa: BLE001
                continue
            scored.append(
                {
                    "source_id": row["source_id"],
                    "source_title": row["title"],
                    "title": row["title"],
                    "kind": row["kind"],
                    "chunk_id": row["chunk_id"],
                    "position": row["position"],
                    "text": row["text"],
                    "summary": str(row["text"])[:240],
                    "score": cosine_similarity(query_embedding, embedding),
                }
            )
        scored.sort(key=lambda item: float(item["score"]), reverse=True)
        return {"results": scored[:top_k]}

    def status(self) -> dict[str, Any]:
        self._touch()
        with self._connect() as conn:
            source_count = int(first_value(conn.execute("SELECT COUNT(*) FROM agent_knowledge_sources").fetchone()) or 0)
            chunk_count = int(first_value(conn.execute("SELECT COUNT(*) FROM agent_knowledge_chunks").fetchone()) or 0)
            total_bytes = int(
                first_value(conn.execute("SELECT COALESCE(SUM(size), 0) FROM agent_knowledge_sources").fetchone()) or 0
            )
        return {
            "ok": True,
            "source_count": source_count,
            "chunk_count": chunk_count,
            "total_bytes": total_bytes,
            "cache_loaded": False,
            "idle_cache_seconds": IDLE_CACHE_SECONDS,
        }

    def release_idle_cache(self) -> None:
        if time.time() - self._last_access > IDLE_CACHE_SECONDS:
            self._uploads.clear()

    async def _index_text(
        self,
        *,
        title: str,
        kind: str,
        mime_type: str,
        data: bytes,
        sha256: str,
        text: str,
        embedding_profile: dict[str, Any],
        embedding_client: EmbeddingClient,
    ) -> dict[str, Any]:
        chunks = chunk_text(text)
        if not chunks:
            raise ValueError("document has no readable text")
        self._check_chunk_capacity(len(chunks))
        embeddings = await embedding_client(embedding_profile, chunks)
        if len(embeddings) != len(chunks):
            raise ValueError("embedding response count mismatch")
        source_id = uuid.uuid4().hex
        now = int(time.time())
        (self.files_dir / sha256).write_bytes(data)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_knowledge_sources
                (source_id, title, kind, mime_type, size, sha256, status, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'ready', '', ?, ?)
                """,
                (source_id, title, kind, mime_type, len(data), sha256, now, now),
            )
            for position, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=True)):
                chunk_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO agent_knowledge_chunks (chunk_id, source_id, position, text, embedding_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        source_id,
                        position,
                        chunk,
                        "[]" if self.vector_index.available else json.dumps(embedding),
                    ),
                )
                self.vector_index.add(
                    {
                        "chunk_id": chunk_id,
                        "source_id": source_id,
                        "title": title,
                        "kind": kind,
                        "position": position,
                        "text": chunk,
                        "summary": chunk[:240],
                        "vector": [float(item) for item in embedding],
                    }
                )
        return self._source_by_id(source_id)

    def _source_by_id(self, source_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT s.*, COALESCE(c.chunk_count, 0) AS chunk_count
                FROM agent_knowledge_sources s
                LEFT JOIN (
                    SELECT source_id, COUNT(*) AS chunk_count
                    FROM agent_knowledge_chunks
                    GROUP BY source_id
                ) c ON c.source_id = s.source_id
                WHERE s.source_id = ?
                """,
                (source_id,),
            ).fetchone()
        if not row:
            raise ValueError("source not found")
        return source_dict(row)

    def _upload(self, params: dict[str, Any]) -> dict[str, Any]:
        upload_id = str(params.get("upload_id") or "").strip()
        upload = self._uploads.get(upload_id)
        if not upload:
            raise ValueError("upload not found")
        return upload

    def _check_capacity(self, incoming_bytes: int) -> None:
        with self._connect() as conn:
            total = int(first_value(conn.execute("SELECT COALESCE(SUM(size), 0) FROM agent_knowledge_sources").fetchone()) or 0)
        if total + incoming_bytes > self.max_total_bytes:
            raise ValueError("knowledge base exceeds max_total_bytes")

    def _check_chunk_capacity(self, incoming_chunks: int) -> None:
        with self._connect() as conn:
            total = int(first_value(conn.execute("SELECT COUNT(*) FROM agent_knowledge_chunks").fetchone()) or 0)
        if total + incoming_chunks > self.max_chunks:
            raise ValueError("knowledge base exceeds max_chunks")

    def _touch(self) -> None:
        self._last_access = time.time()


class LanceVectorIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.available = False
        self._db: Any | None = None
        try:
            import lancedb  # type: ignore[import-not-found]

            self._db = lancedb.connect(str(path))
            self.available = True
        except Exception:
            self._db = None
            self.available = False

    def add(self, row: dict[str, Any]) -> None:
        if not self.available or self._db is None:
            return
        try:
            table = self._open_table()
            if table is None:
                self._db.create_table("chunks", data=[row])
                return
            table.add([row])
        except Exception:
            self.available = False

    def search(self, query_embedding: list[float], source_ids: list[str], top_k: int) -> list[dict[str, Any]] | None:
        if not self.available or self._db is None:
            return None
        try:
            table = self._open_table()
            if table is None:
                return []
            query = table.search([float(item) for item in query_embedding]).limit(top_k)
            if source_ids:
                allowed = {safe_lance_literal(item) for item in source_ids if item}
                if allowed:
                    query = query.where("source_id IN (" + ", ".join(sorted(allowed)) + ")")
            rows = query.to_list()
        except Exception:
            return None
        results: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            distance = row.get("_distance")
            score = None
            if isinstance(distance, (int, float)):
                score = 1.0 / (1.0 + float(distance))
            results.append(
                {
                    "source_id": row.get("source_id"),
                    "source_title": row.get("title"),
                    "title": row.get("title"),
                    "kind": row.get("kind"),
                    "chunk_id": row.get("chunk_id"),
                    "position": row.get("position"),
                    "text": row.get("text") or "",
                    "summary": row.get("summary") or str(row.get("text") or "")[:240],
                    "score": score,
                }
            )
        return results

    def delete_source(self, source_id: str) -> None:
        if not self.available or self._db is None or not source_id:
            return
        try:
            table = self._open_table()
            if table is not None:
                table.delete(f"source_id = {safe_lance_literal(source_id)}")
        except Exception:
            self.available = False

    def _open_table(self) -> Any | None:
        if self._db is None:
            return None
        try:
            return self._db.open_table("chunks")
        except Exception:
            return None


class PostgresConnection:
    def __init__(self, database_url: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ModuleNotFoundError as exc:
            raise RuntimeError("psycopg is required for AGENT_KNOWLEDGE_DATABASE_URL") from exc
        self._conn = psycopg.connect(database_url, row_factory=dict_row)

    def __enter__(self) -> "PostgresConnection":
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return self._conn.__exit__(exc_type, exc, tb)

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        return self._conn.execute(postgres_query(query), params)

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            statement = statement.strip()
            if statement:
                self.execute(statement)


def postgres_query(query: str) -> str:
    query = query.replace("INSERT OR IGNORE INTO", "INSERT INTO")
    query = query.replace("?", "%s")
    query = query.replace("VALUES (1, 0, %s)", "VALUES (1, 0, %s) ON CONFLICT (id) DO NOTHING")
    return query


async def openai_compatible_embeddings(profile: dict[str, Any], texts: list[str]) -> list[list[float]]:
    provider = str(profile.get("provider") or "openai").strip()
    base_url = str(profile.get("base_url") or "").strip().rstrip("/")
    model = str(profile.get("model") or "").strip()
    api_key = str(profile.get("api_key") or "").strip()
    if not base_url:
        base_url = default_base_url(provider)
    if not base_url or not model or not api_key:
        raise ValueError("embedding_profile provider, base_url, model, and api_key are required")
    url = base_url if base_url.endswith("/embeddings") else f"{base_url}/v1/embeddings"
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "input": texts},
        )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("embedding provider returned invalid data")
    vectors: list[list[float]] = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("embedding"), list):
            vectors.append([float(item) for item in row["embedding"]])
    if len(vectors) != len(texts):
        raise ValueError("embedding provider returned incomplete data")
    return vectors


async def openai_compatible_ocr(profile: dict[str, Any], data: bytes, filename: str, mime_type: str) -> str:
    provider = str(profile.get("provider") or "openai").strip()
    base_url = str(profile.get("base_url") or "").strip().rstrip("/") or default_base_url(provider)
    model = str(profile.get("model") or "").strip()
    api_key = str(profile.get("api_key") or "").strip()
    if not base_url or not model or not api_key:
        raise ValueError("ocr_profile provider, base_url, model, and api_key are required")
    url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/v1/chat/completions"
    encoded = base64.b64encode(data).decode("ascii")
    data_url = f"data:{mime_type};base64,{encoded}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"Extract readable text from {filename}. Return only the extracted text.",
                            },
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
            },
        )
    response.raise_for_status()
    payload = response.json()
    try:
        return str(payload["choices"][0]["message"]["content"]).strip()
    except Exception as exc:  # noqa: BLE001
        raise ValueError("ocr provider returned invalid data") from exc


async def extract_text(
    data: bytes,
    filename: str,
    mime_type: str,
    ocr_profile: Any,
    ocr_client: OCRClient,
) -> str:
    if requires_ocr(filename, mime_type):
        profile = require_ocr_profile(ocr_profile)
        return await ocr_client(profile, data, filename, mime_type)
    return data.decode("utf-8", errors="replace")


def chunk_text(text: str) -> list[str]:
    normalized = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()
    if not normalized:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + CHUNK_SIZE)
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end == len(normalized):
            break
        start = max(0, end - CHUNK_OVERLAP)
    return chunks


def require_embedding_profile(raw: Any) -> dict[str, Any]:
    if not embedding_profile_ready(raw):
        raise ValueError("embedding_profile is required")
    return normalize_profile(raw)


def require_ocr_profile(raw: Any) -> dict[str, Any]:
    if not ocr_profile_ready(raw):
        raise ValueError("ocr_profile is required")
    return normalize_profile(raw)


def embedding_profile_ready(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    profile = normalize_profile(raw)
    return bool(profile.get("provider") and profile.get("base_url") and profile.get("model") and profile.get("api_key"))


def ocr_profile_ready(raw: Any) -> bool:
    return embedding_profile_ready(raw)


def normalize_profile(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    provider = str(raw.get("provider") or "").strip()
    base_url = str(raw.get("base_url") or "").strip().rstrip("/")
    if provider and not base_url:
        base_url = default_base_url(provider)
    return {
        "provider": provider,
        "base_url": base_url,
        "model": str(raw.get("model") or raw.get("model_id") or "").strip(),
        "api_key": str(raw.get("api_key") or "").strip(),
    }


def default_base_url(provider: str) -> str:
    normalized = provider.lower().strip()
    return {
        "openai": "https://api.openai.com",
        "deepseek": "https://api.deepseek.com",
        "siliconflow": "https://api.siliconflow.cn",
        "moonshot": "https://api.moonshot.cn",
        "baidu": "https://qianfan.baidubce.com",
    }.get(normalized, "")


def is_supported_document(filename: str, mime_type: str) -> bool:
    return is_text_document(filename, mime_type) or requires_ocr(filename, mime_type)


def is_text_document(filename: str, mime_type: str) -> bool:
    ext = Path(filename).suffix.lower()
    normalized = mime_type.lower()
    return normalized.startswith("text/") or normalized in {
        "application/json",
        "application/x-ndjson",
        "application/xml",
        "application/yaml",
        "text/markdown",
    } or ext in {".txt", ".md", ".markdown", ".csv", ".json", ".log", ".xml", ".yaml", ".yml"}


def requires_ocr(filename: str, mime_type: str) -> bool:
    ext = Path(filename).suffix.lower()
    normalized = mime_type.lower()
    return normalized.startswith("image/") or normalized == "application/pdf" or ext in {
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".bmp",
    }


def guess_mime(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in {".md", ".markdown"}:
        return "text/markdown"
    if ext in {".txt", ".log", ".csv"}:
        return "text/plain"
    if ext == ".json":
        return "application/json"
    if ext == ".pdf":
        return "application/pdf"
    if ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    return "application/octet-stream"


def sanitize_filename(value: str) -> str:
    name = os.path.basename(value.strip()) or "document.txt"
    return "".join(ch if ch.isalnum() or ch in " ._-()" else "_" for ch in name)[:160] or "document.txt"


def safe_lance_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def source_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "source_id": row["source_id"],
        "title": row["title"],
        "kind": row["kind"],
        "mime_type": row["mime_type"],
        "size": row["size"],
        "sha256": row["sha256"],
        "status": row["status"],
        "error": row["error"],
        "chunk_count": int(row["chunk_count"] or 0),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def first_value(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()), None)
    return row[0]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    size = min(len(left), len(right))
    if size == 0:
        return 0.0
    dot = sum(left[index] * right[index] for index in range(size))
    left_norm = math.sqrt(sum(left[index] * left[index] for index in range(size)))
    right_norm = math.sqrt(sum(right[index] * right[index] for index in range(size)))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)
