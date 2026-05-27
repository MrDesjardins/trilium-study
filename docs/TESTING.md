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
3. Confirm a long normalized lesson does not produce a short summary script and that estimated narration length is visible in lesson details.
4. Re-run a completed lesson and confirm unchanged stages are skipped.
5. Force regenerate a lesson and confirm artifacts are replaced.
6. Review flashcards in the UI and confirm due-date updates persist.
7. Clear the due queue, then confirm browse mode still exposes the full deck and reset requeues every card.
8. Intentionally fail a recoverable pipeline dependency once and confirm the lesson auto-retries, then remains resumable if retries are exhausted.

## Gaps

Current tests do not yet execute live:

- Trilium ETAPI requests
- OpenAI calls
- Kokoro synthesis
- ffmpeg rendering
- YouTube uploads

Those paths require environment-backed operator validation on the target machine.
