from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from openai import OpenAI
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.content import clean_study_text
from app.models import Flashcard, FlashcardReview, Lesson, LessonArtifact, YouTubeUpload


STAGES = ["collect", "normalize", "script", "flashcards", "audio", "video", "upload"]

MARKDOWN_NARRATION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?m)^\s{0,3}#{1,6}\s+"), "Markdown heading marker"),
    (re.compile(r"(?m)^\s{0,3}[-*+]\s+"), "Markdown list marker"),
    (re.compile(r"(?m)^\s{0,3}\d+[.)]\s+"), "numbered list marker"),
    (re.compile(r"(?m)^\s{0,3}>\s+"), "Markdown block quote marker"),
    (re.compile(r"```|~~~"), "code fence"),
    (re.compile(r"(?m)^\s*\|.+\|\s*$"), "Markdown table row"),
    (re.compile(r"\[[Aa]dded context\]"), "inline added-context tag"),
)


@dataclass
class GeneratedScript:
    script_text: str
    provenance: dict[str, Any]


@dataclass
class GeneratedFlashcard:
    prompt: str
    answer: str
    source_excerpt: str | None = None


@dataclass
class GeneratedUpload:
    video_id: str
    video_url: str
    response: dict[str, Any]


@dataclass
class ReviewOutcome:
    result: str
    scheduled_due_at: datetime
    reviewed_at: datetime
    next_due_at: datetime
    ease_factor_after: float
    interval_days_after: int
    repetitions_after: int


class ScriptResponsePayload(BaseModel):
    script_text: str
    added_context_notes: list[str] = Field(default_factory=list)
    validation_summary: str = ""
    corrected_or_clarified_points: list[str] = Field(default_factory=list)
    filled_gap_topics: list[str] = Field(default_factory=list)


class FlashcardPayload(BaseModel):
    prompt: str
    answer: str
    source_excerpt: str | None = None


class FlashcardResponsePayload(BaseModel):
    cards: list[FlashcardPayload] = Field(default_factory=list)


class ScriptGenerator(Protocol):
    def generate(self, lesson_title: str, normalized_text: str) -> GeneratedScript: ...


class FlashcardGenerator(Protocol):
    def generate(self, lesson_title: str, script_text: str) -> list[GeneratedFlashcard]: ...


class TTSGenerator(Protocol):
    def generate(self, lesson: Lesson, script_text: str, output_path: Path) -> dict[str, Any]: ...


class VideoRenderer(Protocol):
    def render(self, lesson: Lesson, audio_path: Path, output_path: Path) -> dict[str, Any]: ...


class YouTubePublisher(Protocol):
    def upload(self, lesson: Lesson, video_path: Path) -> GeneratedUpload: ...


class DefaultScriptGenerator:
    max_generation_attempts = 4

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

    def build_prompt(self, lesson_title: str, normalized_text: str) -> tuple[str, str]:
        cleaned_source_text = cleaned_source_material(normalized_text)
        normalized_word_count = script_word_count(cleaned_source_text)
        minimum_words = minimum_script_words(normalized_word_count)
        quality_floor_minutes = estimate_narration_minutes(minimum_words)
        target_minutes = target_duration_minutes(normalized_word_count)
        system = (
            "You are a careful teaching editor and factual validator for study notes. "
            "First, validate the source notes for factual mistakes, misleading shortcuts, missing prerequisites, "
            "and places where the wording would confuse a beginner. "
            "Second, produce a detailed teaching script that preserves the source meaning while correcting errors, "
            "filling only bounded gaps required for accurate understanding, and explaining ideas in simple language. "
            "The script must be beginner-friendly, specific, concrete, and polished enough to study from directly as spoken narration. "
            "Define terms, unpack claims, explain why each idea matters, and use examples, analogies, comparisons, and short restatements generously whenever they improve comprehension. "
            "It is acceptable to repeat an idea in clearer words, approach it from another angle, or add another example if that helps the learner understand and review the material. "
            "Do not compress a content-rich lesson into a short summary. Treat the source as the backbone of a spoken lesson and walk through the major ideas thoroughly so the listener could learn from the script alone. "
            "Do not pad with fluff or motivational filler. Do not invent controversial claims. If a source claim looks doubtful, correct it cautiously. "
            "Any material not directly present in the notes but added to make the lesson accurate or understandable must be summarized in the structured added-context fields, not tagged inline in the spoken script."
        )
        user = (
            f"Lesson title: {lesson_title}\n\n"
            "Use the following source material as the base lesson. Keep source meaning intact, but do not preserve source mistakes.\n"
            "Return JSON with keys:\n"
            "- script_text: a detailed plain-spoken lesson script written in simple language for a beginner\n"
            "- added_context_notes: a short list of major added-context items\n"
            "- validation_summary: one short paragraph summarizing whether the notes were sound or needed correction\n"
            "- corrected_or_clarified_points: a list of important corrections or clarifications you made\n"
            "- filled_gap_topics: a list of small missing topics you added so the lesson is understandable\n\n"
            "Script requirements:\n"
            "- organize the script as a real lesson with a short introduction, section-by-section teaching through the source, and a closing recap\n"
            "- write script_text as natural narration only, with no Markdown, bullets, numbered lists, headings, tables, code fences, decorative separators, bracket labels, or inline tags\n"
            "- do not include literal formatting symbols that a text-to-speech narrator might read aloud, such as asterisk, dash, hash, greater-than, backtick, or vertical bar\n"
            "- if you need transitions, write them as normal sentences instead of list markers or headings\n"
            "- teach the material step by step in plain English\n"
            "- prefer short paragraphs and explicit definitions\n"
            "- make the explanation more detailed than the source notes when the notes are terse\n"
            "- explain assumptions and transitions instead of jumping between ideas\n"
            "- for each major point, include enough explanation, examples, and restatements that the listener can understand it without seeing the original notes\n"
            "- optimize for study quality, comprehension, and review value rather than brevity\n"
            "- it is good to revisit a difficult idea from two angles if that makes it easier to learn\n"
            "- for abstract or difficult claims, add at least one concrete example, analogy, or comparison unless the notes already provide one\n"
            "- when a section introduces several ideas, explain them one by one instead of collapsing them into a dense paragraph\n"
            "- if the notes are incomplete, add only the minimum accurate context needed, include it naturally in the narration, and list it in added_context_notes\n\n"
            "Length requirements:\n"
            f"- the source contains about {normalized_word_count} words\n"
            f"- produce enough explanation to comfortably study from the notes, which usually means at least about {minimum_words} words for a lesson of this size\n"
            f"- aim for roughly {target_minutes[0]} to {target_minutes[1]} minutes of spoken explanation when the source has enough substance\n"
            f"- anything below about {quality_floor_minutes:.1f} minutes will usually be too compressed for this lesson unless the source is unusually repetitive\n"
            "- the lower bound is a quality floor, not the true goal; prefer a richer, easier-to-learn script when the material benefits from more explanation\n"
            "- retain and explain all major source sections instead of replacing them with a brief overview\n\n"
            f"{cleaned_source_text}"
        )
        return system, user

    def generate(self, lesson_title: str, normalized_text: str) -> GeneratedScript:
        if not self.client:
            raise RuntimeError(
                "OPENAI_API_KEY is required for validated script generation. Configure it in .env so notes can be checked and expanded safely."
            )

        cleaned_source_text = cleaned_source_material(normalized_text)
        system, base_user = self.build_prompt(lesson_title, cleaned_source_text)
        normalized_word_count = script_word_count(cleaned_source_text)
        minimum_words = minimum_script_words(normalized_word_count)
        target_minutes = target_duration_minutes(normalized_word_count)
        last_error = "OpenAI returned no parsed script payload."
        previous_word_count: int | None = None

        for attempt in range(1, self.max_generation_attempts + 1):
            user = base_user
            if attempt > 1:
                previous_text = f"Previous draft was about {previous_word_count} words. " if previous_word_count else ""
                if "not plain spoken narration" in last_error:
                    user += (
                        f"\n{previous_text}Previous attempt failed narration formatting validation: {last_error}. "
                        "Rewrite the script as ordinary paragraphs only, without Markdown syntax, bullets, numbered lists, headings, tables, code fences, bracket labels, or inline tags.\n"
                    )
                else:
                    user += (
                        f"\n{previous_text}Previous attempt was too short for the lesson size. "
                        f"Expand the script so it clearly teaches the full lesson and reaches at least about {minimum_words} words unless the source is genuinely repetitive. "
                        "For each important point, add more concrete examples, clearer definitions, comparisons, and alternate phrasings so the lesson becomes easier to study from. "
                        "Do not merely restate headings or compress multiple sections into one paragraph. "
                        "Lean toward richer explanation and review-friendly repetition rather than a polished short summary.\n"
                    )
            if attempt >= 2:
                user += (
                    "Coverage requirements for this retry:\n"
                    "- preserve the source order and teach each major section separately\n"
                    "- include at least one comprehension aid per major section: an example, analogy, comparison, or simpler restatement\n"
                    "- if a concept is highly abstract, explain it once formally and once in simpler everyday language\n"
                    "- add brief recap sentences at the end of major sections so the learner can review while listening\n"
                )
            if attempt >= 3:
                user += (
                    "Make the script feel like study material, not a summary:\n"
                    "- slow down the explanation of difficult ideas\n"
                    "- turn compact claims into short teachable sequences of 2 to 4 sentences\n"
                    "- prefer extra clarification over concision whenever there is a tradeoff\n"
                )
            if attempt >= 4:
                user += (
                    "Final retry requirement:\n"
                    "- be intentionally expansive where needed for comprehension\n"
                    "- if the lesson is still under target, add another example or alternate explanation for each major source section rather than shortening the whole lesson\n"
                )
            response = self.client.responses.parse(
                model=self.settings.openai_model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                text_format=ScriptResponsePayload,
            )
            payload = response.output_parsed
            if payload is None:
                continue
            markup_issues = narration_markup_issues(payload.script_text)
            if markup_issues:
                last_error = "Generated script was not plain spoken narration: " + ", ".join(markup_issues)
                previous_word_count = script_word_count(payload.script_text)
                continue
            generated_word_count = script_word_count(payload.script_text)
            previous_word_count = generated_word_count
            estimated_minutes = estimate_narration_minutes(generated_word_count)
            if not script_meets_length_target(normalized_word_count, generated_word_count):
                last_error = (
                    "Generated script was too compressed for the lesson size "
                    f"({generated_word_count} words from about {normalized_word_count} source words, "
                    f"estimated {estimated_minutes:.1f} minutes; target about {target_minutes[0]}-{target_minutes[1]} minutes)."
                )
                continue
            return GeneratedScript(
                script_text=payload.script_text,
                provenance={
                    "mode": "openai",
                    "added_context_notes": payload.added_context_notes,
                    "validation_summary": payload.validation_summary,
                    "corrected_or_clarified_points": payload.corrected_or_clarified_points,
                    "filled_gap_topics": payload.filled_gap_topics,
                    "model": self.settings.openai_model,
                    "normalized_word_count": normalized_word_count,
                    "normalized_word_count_raw": script_word_count(normalized_text),
                    "script_word_count": generated_word_count,
                    "script_minimum_words": minimum_words,
                    "estimated_narration_minutes": round(estimated_minutes, 2),
                    "target_duration_minutes": list(target_minutes),
                    "generation_attempts": attempt,
                },
            )
        raise RuntimeError(last_error)


class DefaultFlashcardGenerator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

    def generate(self, lesson_title: str, script_text: str) -> list[GeneratedFlashcard]:
        if not self.client:
            lines = [line.strip() for line in script_text.splitlines() if line.strip()]
            cards: list[GeneratedFlashcard] = []
            for line in lines[:5]:
                cards.append(GeneratedFlashcard(prompt=f"What is the key idea of: {line[:48]}?", answer=line, source_excerpt=line))
            return cards

        response = self.client.responses.parse(
            model=self.settings.openai_model,
            input=[
                {"role": "system", "content": "Create concise flashcards from the script. Return JSON array of prompt, answer, source_excerpt."},
                {"role": "user", "content": f"Lesson: {lesson_title}\n\n{script_text}"},
            ],
            text_format=FlashcardResponsePayload,
        )
        payload = response.output_parsed
        if payload is None:
            raise RuntimeError("OpenAI returned no parsed flashcard payload.")
        return [GeneratedFlashcard(**item.model_dump()) for item in payload.cards]


class CommandTTSGenerator:
    def __init__(self, settings: Settings):
        self.settings = settings

    def generate(self, lesson: Lesson, script_text: str, output_path: Path) -> dict[str, Any]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.settings.kokoro_command:
            return self._generate_builtin(lesson, script_text, output_path)
        script_file = output_path.with_suffix(".txt")
        script_file.write_text(script_text, encoding="utf-8")
        command = self.settings.kokoro_command.format(input=script_file, output=output_path)
        subprocess.run(command, shell=True, check=True)
        return {"mode": "kokoro", "path": str(output_path), "duration_seconds": media_duration_seconds(output_path)}

    def _generate_builtin(self, lesson: Lesson, script_text: str, output_path: Path) -> dict[str, Any]:
        configure_tts_runtime_warnings(self.settings.hf_token)
        try:
            import numpy as np
            import soundfile as sf
            from kokoro import KPipeline
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Kokoro dependencies are not installed. Run `uv sync --extra tts`, or set KOKORO_COMMAND to a custom command."
            ) from exc

        pipeline = KPipeline(lang_code=self.settings.kokoro_lang_code)
        segments: list[Any] = []
        pieces = 0
        for pieces, (_, _, audio) in enumerate(
            pipeline(
                script_text,
                voice=self.settings.kokoro_voice,
                speed=self.settings.kokoro_speed,
                split_pattern=r"\n+",
            ),
            start=1,
        ):
            segments.append(audio)
        if not segments:
            raise RuntimeError(f"Kokoro did not generate any audio for lesson {lesson.title!r}")
        audio_array = np.concatenate(segments)
        sf.write(output_path, audio_array, 24000)
        return {
            "mode": "kokoro-python",
            "path": str(output_path),
            "duration_seconds": round(len(audio_array) / 24000, 3),
            "voice": self.settings.kokoro_voice,
            "lang_code": self.settings.kokoro_lang_code,
            "speed": self.settings.kokoro_speed,
            "segments": pieces,
        }


class FfmpegVideoRenderer:
    def __init__(self, settings: Settings):
        self.settings = settings

    def render(self, lesson: Lesson, audio_path: Path, output_path: Path) -> dict[str, Any]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        duration_seconds = media_duration_seconds(audio_path)
        command = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            (
                f"color=c={self.settings.video_background}:"
                f"s={self.settings.video_width}x{self.settings.video_height}:"
                f"d={duration_seconds:.3f}"
            ),
            "-i",
            str(audio_path),
            "-vf",
            f"drawtext=text='{self._escape(lesson.title)}':fontcolor=0x2b2316:fontsize=40:x=(w-text_w)/2:y=(h-text_h)/2",
            "-shortest",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(output_path),
        ]
        subprocess.run(command, check=True)
        return {"mode": "ffmpeg", "path": str(output_path), "duration_seconds": duration_seconds}

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("'", r"\'").replace(":", r"\:")


def media_duration_seconds(path: Path) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        detail = stderr or str(exc)
        raise RuntimeError(f"ffprobe could not determine media duration for {path}: {detail}") from exc

    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"ffprobe returned an invalid media duration for {path}: {result.stdout!r}") from exc
    if duration <= 0:
        raise RuntimeError(f"Media duration must be positive for {path}, got {duration}")
    return duration


class YouTubeApiPublisher:
    scope = "https://www.googleapis.com/auth/youtube.upload"

    def __init__(self, settings: Settings):
        self.settings = settings

    def upload(self, lesson: Lesson, video_path: Path) -> GeneratedUpload:
        if not self.settings.youtube_token_file:
            raise RuntimeError("YOUTUBE_TOKEN_FILE is not configured")
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from googleapiclient.errors import HttpError
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("YouTube dependencies are not installed. Run `uv sync --extra youtube`.") from exc

        token_path = Path(self.settings.youtube_token_file).expanduser().resolve()
        if not token_path.exists():
            raise RuntimeError(f"YouTube token file not found: {token_path}")

        credentials = Credentials.from_authorized_user_file(str(token_path), [self.scope])
        if not credentials.valid:
            if credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
                token_path.write_text(credentials.to_json(), encoding="utf-8")
            else:
                raise RuntimeError("YouTube credentials are invalid and cannot be refreshed. Re-run app.youtube_auth.")

        youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        request = youtube.videos().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": lesson.title,
                    "description": "Generated by Trilium Study from a lesson note.",
                    "categoryId": "27",
                },
                "status": {"privacyStatus": "unlisted"},
            },
            media_body=MediaFileUpload(str(video_path), chunksize=-1, resumable=False),
        )
        try:
            response = request.execute()
        except HttpError as exc:
            raise RuntimeError(self._format_upload_error(exc)) from exc
        video_id = response["id"]
        return GeneratedUpload(
            video_id=video_id,
            video_url=f"https://www.youtube.com/watch?v={video_id}",
            response=response,
        )

    @staticmethod
    def _format_upload_error(exc) -> str:
        status = getattr(getattr(exc, "resp", None), "status", "unknown")
        body = ""
        content = getattr(exc, "content", b"")
        if isinstance(content, bytes):
            body = content.decode("utf-8", errors="replace")
        else:
            body = str(content)
        if "accessNotConfigured" in body or "youtube.googleapis.com/overview" in body:
            return (
                "YouTube Data API v3 is disabled for the Google Cloud project backing this OAuth client. "
                "Enable it in Google Cloud Console for this project, wait a few minutes, then click Generate again to resume from upload."
            )
        return f"YouTube upload failed with HTTP {status}: {body}"


class StubYouTubePublisher:
    def upload(self, lesson: Lesson, video_path: Path) -> GeneratedUpload:
        slug = re.sub(r"[^a-z0-9]+", "-", lesson.title.lower()).strip("-") or "lesson"
        video_id = f"stub-{lesson.id}-{slug}"
        return GeneratedUpload(
            video_id=video_id,
            video_url=f"https://youtube.com/watch?v={video_id}",
            response={"mode": "stub", "video_path": str(video_path)},
        )


def ensure_artifact(session: Session, lesson: Lesson, artifact_type: str, stage: str, content_hash: str) -> LessonArtifact:
    artifact = session.scalar(
        select(LessonArtifact).where(LessonArtifact.lesson_id == lesson.id, LessonArtifact.artifact_type == artifact_type)
    )
    if artifact is None:
        artifact = LessonArtifact(
            lesson_id=lesson.id,
            artifact_type=artifact_type,
            stage=stage,
            state="pending",
            content_hash=content_hash,
        )
        session.add(artifact)
    artifact.stage = stage
    artifact.content_hash = content_hash
    return artifact


def should_skip_stage(session: Session, lesson: Lesson, artifact_type: str, stage: str, content_hash: str, force: bool) -> bool:
    if force:
        return False
    artifact = session.scalar(
        select(LessonArtifact).where(LessonArtifact.lesson_id == lesson.id, LessonArtifact.artifact_type == artifact_type)
    )
    return bool(artifact and artifact.state == "completed" and artifact.content_hash == content_hash)


def replace_flashcards(session: Session, lesson: Lesson, cards: list[GeneratedFlashcard]) -> None:
    for card in list(lesson.flashcards):
        session.delete(card)
    now = datetime.now(timezone.utc)
    for card in cards:
        session.add(
            Flashcard(
                lesson_id=lesson.id,
                prompt=card.prompt,
                answer=card.answer,
                source_excerpt=card.source_excerpt,
                due_at=now,
            )
        )


def schedule_flashcard_review(flashcard: Flashcard, result: str, reviewed_at: datetime | None = None) -> ReviewOutcome:
    reviewed_at = reviewed_at or datetime.now(timezone.utc)
    scheduled_due = flashcard.due_at
    ease = flashcard.ease_factor
    reps = flashcard.repetitions
    interval = flashcard.interval_days

    if result == "again":
        reps = 0
        interval = 1
        ease = max(1.3, ease - 0.2)
    elif result == "hard":
        # Card advanced but with friction: barely grow the interval, lower ease.
        reps += 1
        interval = max(1, round(max(interval, 1) * 1.2))
        ease = max(1.3, ease - 0.15)
    elif result == "easy":
        reps += 1
        if reps == 1:
            interval = 2
        elif reps == 2:
            interval = 5
        else:
            interval = max(1, round(interval * ease * 1.3))
        ease = min(3.0, ease + 0.15)
    else:
        reps += 1
        if reps == 1:
            interval = 1
        elif reps == 2:
            interval = 3
        else:
            interval = max(1, round(interval * ease))
        ease = min(3.0, ease + 0.05)

    next_due = reviewed_at + timedelta(days=interval)
    flashcard.ease_factor = ease
    flashcard.repetitions = reps
    flashcard.interval_days = interval
    flashcard.due_at = next_due
    return ReviewOutcome(
        result=result,
        scheduled_due_at=scheduled_due,
        reviewed_at=reviewed_at,
        next_due_at=next_due,
        ease_factor_after=ease,
        interval_days_after=interval,
        repetitions_after=reps,
    )


def create_flashcard_review(flashcard: Flashcard, outcome: ReviewOutcome) -> FlashcardReview:
    return FlashcardReview(
        flashcard=flashcard,
        result=outcome.result,
        scheduled_due_at=outcome.scheduled_due_at,
        reviewed_at=outcome.reviewed_at,
        next_due_at=outcome.next_due_at,
        ease_factor_after=outcome.ease_factor_after,
        interval_days_after=outcome.interval_days_after,
        repetitions_after=outcome.repetitions_after,
    )


def upsert_youtube_upload(session: Session, lesson: Lesson, upload: GeneratedUpload) -> None:
    existing = session.scalar(select(YouTubeUpload).where(YouTubeUpload.lesson_id == lesson.id))
    if existing is None:
        existing = YouTubeUpload(
            lesson_id=lesson.id,
            video_id=upload.video_id,
            video_url=upload.video_url,
            response_json=json.dumps(upload.response, ensure_ascii=True),
        )
        session.add(existing)
    else:
        existing.video_id = upload.video_id
        existing.video_url = upload.video_url
        existing.response_json = json.dumps(upload.response, ensure_ascii=True)


def default_kokoro_command() -> str:
    return f"{sys.executable} -m app.kokoro_cli --input {{input}} --output {{output}}"


def configure_tts_runtime_warnings(hf_token: str | None = None) -> None:
    if hf_token:
        os.environ.setdefault("HF_TOKEN", hf_token)
        os.environ.setdefault("HUGGING_FACE_HUB_TOKEN", hf_token)
    warnings.filterwarnings(
        "ignore",
        message="dropout option adds dropout after all but last recurrent layer.*",
        category=UserWarning,
        module=r"torch\.nn\.modules\.rnn",
    )
    warnings.filterwarnings(
        "ignore",
        message=r"`torch\.nn\.utils\.weight_norm` is deprecated.*",
        category=FutureWarning,
        module=r"torch\.nn\.utils\.weight_norm",
    )
    try:
        import torch
    except ImportError:
        return
    nnpack = getattr(getattr(torch, "backends", None), "nnpack", None)
    if nnpack is not None and hasattr(nnpack, "set_flags"):
        nnpack.set_flags(False)


def script_word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text))


def narration_markup_issues(text: str) -> list[str]:
    issues: list[str] = []
    for pattern, label in MARKDOWN_NARRATION_PATTERNS:
        if pattern.search(text):
            issues.append(label)
    return issues


def cleaned_source_material(text: str) -> str:
    return clean_study_text(text)


def estimate_narration_minutes(word_count: int) -> float:
    if word_count <= 0:
        return 0.0
    return word_count / 145.0


def target_duration_minutes(normalized_word_count: int) -> tuple[int, int]:
    if normalized_word_count >= 7000:
        return (8, 25)
    if normalized_word_count >= 3500:
        return (8, 18)
    if normalized_word_count >= 1800:
        return (7, 14)
    if normalized_word_count >= 900:
        return (5, 10)
    return (4, 8)


def minimum_script_words(normalized_word_count: int) -> int:
    if normalized_word_count < 400:
        return max(250, round(normalized_word_count * 0.65))
    if normalized_word_count < 1000:
        return max(500, round(normalized_word_count * 0.65))
    if normalized_word_count < 2500:
        return max(700, round(normalized_word_count * 0.48))
    if normalized_word_count < 5000:
        return max(950, round(normalized_word_count * 0.30))
    return min(1800, max(1200, round(normalized_word_count * 0.16)))


def script_meets_length_target(normalized_word_count: int, generated_word_count: int) -> bool:
    return generated_word_count >= minimum_script_words(normalized_word_count)
