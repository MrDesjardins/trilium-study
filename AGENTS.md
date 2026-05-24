# Repository Agent Rules

## Documentation

- Always keep architecture and operations documentation in Markdown under `docs/`.
- When the application structure, pipeline stages, deployment flow, or external integrations change, update `docs/ARCHITECTURE.md`.
- When validation strategy, test coverage, or verification commands change, update `docs/TESTING.md`.
- When installation, deployment, runtime dependencies, or recovery procedures change, update `docs/OPERATIONS.md`.
- Do not leave significant implementation changes undocumented.

## Quality Gates

- Prefer production-complete implementations over placeholders. If a stage is intentionally stubbed, document the limitation in code and in `docs/ARCHITECTURE.md`.
- Before finishing, run the narrowest meaningful automated verification and report what was and was not verified.
- For pipeline changes, verify failure handling, rerun behavior, and persisted state assumptions.
- For deployment changes, ensure the install path is scripted and idempotent.
- For configuration-driven integrations, fail with actionable errors instead of silent fallbacks.
- Do not put transient UI status or error messages into query strings. Use durable server-side or cookie-backed flash state so reloads and copied URLs remain clean.
- Flashcard and study interfaces must remain mobile-friendly by default, with touch-safe controls, readable typography, and layouts that work on narrow screens without horizontal overflow.
- When using utility/state classes such as `hidden`, `active`, or `disabled`, verify that component-level CSS cannot override them. Hidden UI must not render by default.
- Keep secrets out of version control and maintain `.gitignore` coverage for local state, credentials, and generated artifacts.

## Reliability

- Preserve resumability. Avoid changes that force expensive stages to rerun without cause.
- Prefer explicit dependency checks and operator-facing error messages for external tools such as `ffmpeg`, `espeak-ng`, Kokoro, and YouTube auth.
- Treat tests, docs, and operational scripts as part of the deliverable, not optional follow-up work.
