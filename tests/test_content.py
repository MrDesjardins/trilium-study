from __future__ import annotations

import asyncio

from app.content import LessonCollector, clean_study_text


def test_normalize_tree_collects_nested_textual_content():
    collector = LessonCollector(trilium_client=None)  # type: ignore[arg-type]
    tree = {
        "note_id": "root",
        "title": "Lesson 1",
        "type": "text",
        "mime": "text/markdown",
        "content": "Overview",
        "children": [
            {"note_id": "a", "title": "Part A", "type": "text", "mime": "text/plain", "content": "Alpha", "children": []},
            {"note_id": "b", "title": "Attachment", "type": "file", "mime": "application/pdf", "content": "ignored", "children": []},
            {
                "note_id": "c",
                "title": "Nested",
                "type": "text",
                "mime": "text/markdown",
                "content": "Gamma",
                "children": [{"note_id": "d", "title": "Deep", "type": "text", "mime": "text/plain", "content": "Delta", "children": []}],
            },
        ],
    }

    normalized = collector._normalize_tree(tree)

    assert "Lesson 1" in normalized
    assert "Alpha" in normalized
    assert "Gamma" in normalized
    assert "Delta" in normalized
    assert "ignored" not in normalized


class FakeClient:
    def __init__(self):
        self.notes = {
            "course": {"noteId": "course", "title": "Course", "childNoteIds": ["lesson-a", "lesson-b"]},
            "lesson-a": {"noteId": "lesson-a", "title": "Lesson A", "childNoteIds": ["child-a"]},
            "lesson-b": {"noteId": "lesson-b", "title": "Lesson B", "childNoteIds": []},
            "child-a": {"noteId": "child-a", "title": "Child A", "childNoteIds": []},
        }
        self.contents = {note_id: note["title"] for note_id, note in self.notes.items()}

    async def get_note(self, note_id: str):
        return self.notes[note_id]

    async def get_note_content(self, note_id: str):
        return self.contents[note_id]

    async def get_child_notes(self, note_id: str):
        return [self.notes[child_id] for child_id in self.notes[note_id].get("childNoteIds", [])]

    async def get_course_lessons(self, parent_note_id: str):
        return await self.get_child_notes(parent_note_id)


def test_course_lessons_use_direct_child_note_ids():
    client = FakeClient()

    lessons = asyncio.run(client.get_course_lessons("course"))

    assert [lesson["noteId"] for lesson in lessons] == ["lesson-a", "lesson-b"]


def test_walk_note_uses_note_children_not_branch_lookup():
    collector = LessonCollector(FakeClient())

    tree = asyncio.run(collector._walk_note("lesson-a"))

    assert tree["note_id"] == "lesson-a"
    assert [child["note_id"] for child in tree["children"]] == ["child-a"]


def test_clean_study_text_strips_html_and_urls():
    cleaned = clean_study_text("<h2>Epistemology</h2><p>Study of knowledge</p><p>See https://example.com</p><ul><li>Truth</li><li>Belief</li></ul>")

    assert "<h2>" not in cleaned
    assert "https://example.com" not in cleaned
    assert "Epistemology" in cleaned
    assert "- Truth" in cleaned
    assert "- Belief" in cleaned


def test_normalize_tree_deduplicates_identical_cleaned_blocks():
    collector = LessonCollector(trilium_client=None)  # type: ignore[arg-type]
    tree = {
        "note_id": "root",
        "title": "Lesson 1",
        "type": "text",
        "mime": "text/html",
        "content": "<p>Repeated idea</p>",
        "children": [
            {
                "note_id": "child",
                "title": "Child",
                "type": "text",
                "mime": "text/html",
                "content": "<div>Repeated idea</div>",
                "children": [],
            }
        ],
    }

    normalized = collector._normalize_tree(tree)

    assert normalized.count("Repeated idea") == 1
