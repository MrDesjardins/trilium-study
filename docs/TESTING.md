# Testing Strategy

## Current Automated Coverage

The repository currently verifies:

- recursive lesson normalization and non-text filtering
- HTML cleanup and duplicate-block suppression during lesson normalization
- script validation-and-expansion prompt policy
- plain-spoken narration validation that rejects Markdown-like script output before audio generation
- comprehension-first retry prompting for examples, restatements, and richer study explanations
- multi-attempt in-stage script escalation before a lesson pipeline retry is consumed
- minimum script-length gating for content-rich lessons using cleaned source text
- reduced middle-band script floors so valid study-length scripts are not rejected on narrow misses
- FSRS review scheduling behavior (grade mapping, learning steps, deterministic fuzz-off scheduling, review snapshots, history-replay seeding, undo restore and refusal paths) in `tests/test_srs.py`
- flashcard generation prompt policy, length-scaled card counts, and history-preserving regeneration matching
- study queue stats, browse mode, and reset behavior
- daily new-card limits, review-tier priority, and the Los Angeles day boundary for "today" stats
- in-review card tools (suspend, unsuspend, edit, delete, undo) over JSON and redirect paths
- study analytics payload (streak, retention, forecast, heatmap) rendering
- audio-queue JSON responses for single lessons and course-wide bulk queueing
- ffmpeg renderer duration alignment
- durable lesson job success path
- durable lesson job failure path
- automatic retry scheduling for transient lesson job failures
- local lesson CLI generation path and workspace path resolution
- catalog-root sync that creates multiple courses
- archive preservation for courses and lessons missing from Trilium sync
- grouped multi-course dashboard API behavior
- archived lesson generation safeguards
- progress and ETA helper behavior

Run the current test suite with:

```bash
uv run --extra dev pytest
```

## Required Validation Mindset

For meaningful changes, verify the smallest relevant set below:

- Unit tests for isolated logic such as parsing, hashing, scheduling, prompt assembly, and artifact state decisions.
- Integration tests for full lesson pipeline progression, reuse, and failure recovery.
- Manual operational checks for Trilium connectivity, Kokoro generation, ffmpeg output, and YouTube upload.

## Recommended Manual Checks

1. Sync the catalog root from Trilium and confirm direct-child course discovery plus per-course direct-child lesson discovery.
2. Run a lesson and confirm script, flashcards, audio, video, and upload metadata are persisted.
3. Confirm a long normalized lesson does not produce a short summary script and that estimated narration length is visible in lesson details.
4. Re-run a completed lesson and confirm unchanged stages are skipped.
5. Force regenerate a lesson and confirm artifacts are replaced.
6. Review flashcards in the UI and confirm due-date updates persist.
7. Clear the due queue, then confirm browse mode still exposes the full deck and reset requeues every card.
8. Intentionally fail a recoverable pipeline dependency once and confirm the lesson auto-retries, then remains resumable if retries are exhausted.
9. Remove a course or lesson from the Trilium catalog hierarchy, sync, and confirm it is hidden but its generated state is not deleted.

## Gaps

Current tests do not yet execute live:

- Trilium ETAPI requests
- OpenAI calls
- Kokoro synthesis
- ffmpeg rendering
- YouTube uploads

Those paths require environment-backed operator validation on the target machine.
