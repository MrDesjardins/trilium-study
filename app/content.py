from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import httpx


TEXT_TYPES = {"text", "code", "book", "render"}


@dataclass
class TriliumNote:
    note_id: str
    title: str
    note_type: str = "text"
    mime: str | None = None
    content: str | None = None
    children: list["TriliumNote"] = field(default_factory=list)

    def is_textual(self) -> bool:
        if self.note_type in TEXT_TYPES:
            return True
        if self.mime and self.mime.startswith("text/"):
            return True
        return False


@dataclass
class LessonSnapshot:
    root_note_id: str
    title: str
    note_tree: dict[str, Any]
    normalized_text: str
    content_hash: str


class TriliumClientError(RuntimeError):
    pass


class TriliumClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": token}
        self.timeout = timeout

    async def _get(self, path: str) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}{path}", headers=self.headers)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise TriliumClientError(f"Trilium request failed for {path}: {response.status_code} {response.text}") from exc
            return response.json()

    async def get_note(self, note_id: str) -> dict[str, Any]:
        return await self._get(f"/etapi/notes/{note_id}")

    async def get_note_content(self, note_id: str) -> str:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/etapi/notes/{note_id}/content", headers=self.headers)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise TriliumClientError(
                    f"Trilium content request failed for /etapi/notes/{note_id}/content: {response.status_code} {response.text}"
                ) from exc
            return response.text

    async def get_child_notes(self, note_id: str) -> list[dict[str, Any]]:
        note = await self.get_note(note_id)
        child_note_ids = note.get("childNoteIds", [])
        return [await self.get_note(child_note_id) for child_note_id in child_note_ids]

    async def get_course_lessons(self, parent_note_id: str) -> list[dict[str, Any]]:
        return await self.get_child_notes(parent_note_id)


class LessonCollector:
    def __init__(self, trilium_client: TriliumClient):
        self.trilium_client = trilium_client

    async def collect_lesson(self, lesson_note_id: str) -> LessonSnapshot:
        root_data = await self.trilium_client.get_note(lesson_note_id)
        note_tree = await self._walk_note(lesson_note_id)
        normalized = self._normalize_tree(note_tree)
        content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return LessonSnapshot(
            root_note_id=lesson_note_id,
            title=root_data.get("title", lesson_note_id),
            note_tree=note_tree,
            normalized_text=normalized,
            content_hash=content_hash,
        )

    async def _walk_note(self, note_id: str) -> dict[str, Any]:
        note = await self.trilium_client.get_note(note_id)
        content = await self.trilium_client.get_note_content(note_id)
        child_notes = await self.trilium_client.get_child_notes(note_id)
        children = [await self._walk_note(child_note["noteId"]) for child_note in child_notes]
        return {
            "note_id": note["noteId"],
            "title": note.get("title", ""),
            "type": note.get("type", "text"),
            "mime": note.get("mime"),
            "content": content,
            "children": children,
        }

    def _normalize_tree(self, node: dict[str, Any]) -> str:
        blocks: list[str] = []

        def visit(current: dict[str, Any], depth: int) -> None:
            note = TriliumNote(
                note_id=current["note_id"],
                title=current.get("title", ""),
                note_type=current.get("type", "text"),
                mime=current.get("mime"),
                content=current.get("content"),
            )
            if note.is_textual():
                header = "#" * min(depth + 1, 6)
                blocks.append(f"{header} {note.title}".strip())
                if note.content:
                    blocks.append(note.content.strip())
            for child in current.get("children", []):
                visit(child, depth + 1)

        visit(node, 1)
        return "\n\n".join(block for block in blocks if block)

    @staticmethod
    def snapshot_json(snapshot: LessonSnapshot) -> str:
        return json.dumps(
            {
                "root_note_id": snapshot.root_note_id,
                "title": snapshot.title,
                "note_tree": snapshot.note_tree,
                "normalized_text": snapshot.normalized_text,
                "content_hash": snapshot.content_hash,
            },
            ensure_ascii=True,
            indent=2,
        )
