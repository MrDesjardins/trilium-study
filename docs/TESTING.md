# Testing Strategy

## Current Automated Coverage

The repository currently verifies:

- recursive lesson normalization and non-text filtering
- script validation-and-expansion prompt policy
- flashcard review scheduling behavior
- study queue stats, browse mode, and reset behavior
- ffmpeg renderer duration alignment
- durable lesson job success path
- durable lesson job failure path
- automatic retry scheduling for transient lesson job failures
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

1. Sync a course from Trilium and confirm direct-child lesson discovery.
2. Run a lesson and confirm script, flashcards, audio, video, and upload metadata are persisted.
3. Re-run a completed lesson and confirm unchanged stages are skipped.
4. Force regenerate a lesson and confirm artifacts are replaced.
5. Review flashcards in the UI and confirm due-date updates persist.
6. Clear the due queue, then confirm browse mode still exposes the full deck and reset requeues every card.
7. Intentionally fail a recoverable pipeline dependency once and confirm the lesson auto-retries, then remains resumable if retries are exhausted.

## Gaps

Current tests do not yet execute live:

- Trilium ETAPI requests
- OpenAI calls
- Kokoro synthesis
- ffmpeg rendering
- YouTube uploads

Those paths require environment-backed operator validation on the target machine.
