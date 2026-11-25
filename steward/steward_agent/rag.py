"""Docs retrieval. One index per collection (admin, help), each built from a folder of
Markdown files. The corpus is tiny, so vectors live in a JSON file and search is a plain
cosine similarity loop: no vector database needed."""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("steward.rag")

INDEX_VERSION = 1
HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")


@dataclass
class Chunk:
    source: str        # file path relative to the collection folder
    title: str         # "Doc title > Section > Subsection"
    text: str


@dataclass
class Hit:
    chunk: Chunk
    score: float

    @property
    def title(self) -> str:
        return self.chunk.title

    @property
    def text(self) -> str:
        return self.chunk.text


def chunk_markdown(text: str, source: str, max_chars: int = 1800) -> list[Chunk]:
    """Split by H1-H3 headings, keeping the heading path as the chunk title. Sections longer
    than max_chars are split on blank lines. Headings inside code fences are ignored."""
    doc_title = Path(source).stem.replace("-", " ").replace("_", " ").strip().capitalize()
    stack: list[tuple[int, str]] = []
    sections: list[tuple[list[str], list[str]]] = []
    body: list[str] = []
    in_fence = False

    def flush():
        if any(line.strip() for line in body):
            sections.append(([h for _, h in stack], list(body)))
        body.clear()

    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            flush()
            level, heading = len(m.group(1)), m.group(2).strip()
            if level == 1 and not stack and not sections:
                doc_title = heading
            stack = [(lv, h) for lv, h in stack if lv < level] + [(level, heading)]
            continue
        body.append(line)
    flush()

    chunks: list[Chunk] = []
    for headings, lines in sections:
        path = [h for h in headings if h != doc_title]
        title = " > ".join([doc_title, *path])
        content = "\n".join(lines).strip()
        for piece in _split(content, max_chars):
            chunks.append(Chunk(source=source, title=title, text=piece))
    return chunks


def _split(content: str, max_chars: int) -> list[str]:
    if len(content) <= max_chars:
        return [content]
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", content):
        if cur and len(cur) + len(para) + 2 > max_chars:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
        while len(cur) > max_chars:  # a single huge paragraph
            out.append(cur[:max_chars])
            cur = cur[max_chars:]
    if cur.strip():
        out.append(cur)
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]


class DocIndex:
    """Index over one docs folder. Rebuilt automatically when files or the model change."""

    def __init__(self, name: str, docs_dir: Path, index_path: Path, embedder: Embedder,
                 model: str, doc_prefix: str = "", query_prefix: str = ""):
        self.name = name
        self.docs_dir = docs_dir
        self.index_path = index_path
        self.embed = embedder
        self.model = model
        self.doc_prefix = doc_prefix
        self.query_prefix = query_prefix
        self._chunks: list[Chunk] = []
        self._vectors: list[list[float]] = []
        self._manifest: dict | None = None

    def _current_manifest(self) -> dict:
        files = []
        if self.docs_dir.is_dir():
            for p in sorted(self.docs_dir.rglob("*.md")):
                if any(part.startswith(".") for part in p.relative_to(self.docs_dir).parts):
                    continue
                st = p.stat()
                files.append([str(p.relative_to(self.docs_dir)), st.st_size, st.st_mtime_ns])
        return {"version": INDEX_VERSION, "model": self.model, "doc_prefix": self.doc_prefix, "files": files}

    @property
    def size(self) -> int:
        return len(self._chunks)

    async def ensure_fresh(self) -> bool:
        """Load or rebuild the index if needed. Returns True if it was rebuilt."""
        manifest = self._current_manifest()
        if self._manifest == manifest:
            return False
        if self._manifest is None and self.index_path.exists():
            try:
                saved = json.loads(self.index_path.read_text(encoding="utf-8"))
                if saved.get("manifest") == manifest:
                    self._chunks = [Chunk(**c) for c in saved["chunks"]]
                    self._vectors = saved["vectors"]
                    self._manifest = manifest
                    log.info("%s index loaded: %d chunks", self.name, self.size)
                    return False
            except (ValueError, KeyError, TypeError):
                log.warning("%s index file unreadable, rebuilding", self.name)

        chunks: list[Chunk] = []
        for rel, _, _ in manifest["files"]:
            text = (self.docs_dir / rel).read_text(encoding="utf-8", errors="replace")
            chunks.extend(chunk_markdown(text, rel))
        vectors: list[list[float]] = []
        for i in range(0, len(chunks), 16):
            batch = chunks[i:i + 16]
            vectors.extend(await self.embed([f"{self.doc_prefix}{c.title}\n{c.text}" for c in batch]))
        self._chunks, self._vectors, self._manifest = chunks, vectors, manifest
        try:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.index_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"manifest": manifest, "chunks": [asdict(c) for c in chunks],
                                       "vectors": vectors}), encoding="utf-8")
            os.replace(tmp, self.index_path)
        except OSError as exc:
            log.warning("could not save %s index (%s); it will be rebuilt on restart", self.name, exc)
        log.info("%s index rebuilt: %d files, %d chunks", self.name, len(manifest["files"]), len(chunks))
        return True

    async def search(self, query: str, top_k: int, min_score: float) -> list[Hit]:
        if not self._chunks:
            return []
        [qv] = await self.embed([f"{self.query_prefix}{query}"])
        scored = sorted((Hit(c, _cosine(qv, v)) for c, v in zip(self._chunks, self._vectors)),
                        key=lambda h: h.score, reverse=True)
        return [h for h in scored[:top_k] if h.score >= min_score]
