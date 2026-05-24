# RTK - Rust Token Killer (Codex CLI)

**Usage**: Token-optimized shell command policy for Codex sessions in this repository.

## Codex Constraint

- Codex does **not** have RTK auto-rewrite hooks in this environment.
- `rtk init --codex` installs instruction files, not a transparent shell interceptor.
- If Codex prints or runs the original command, the original command is what executed.

## Required Rule

- For shell commands with medium or high output, Codex must call the RTK form explicitly.
- Use raw commands only when a fallback rule below applies.
- Treat this file as the source of truth for Codex shell behavior in this repository.

## Use RTK First

Use RTK by default for:

- file reads
- text search output
- git status / diff / log
- lint, typecheck, and test output
- package-manager output
- docker / kubectl logs
- other commands likely to emit more than a few lines

## Preferred Command Mapping

- `git status` -> `rtk git status`
- `git diff` -> `rtk git diff`
- `git log` -> `rtk git log`
- `cat <file>` -> `rtk read <file>`
- `head <file>` -> `rtk read <file>`
- `tail <file>` -> `rtk read <file>`
- `rg <pattern>` -> `rtk grep <pattern> .`
- `grep <pattern>` -> `rtk grep <pattern> .`
- `ls <path>` -> `rtk ls <path>`
- `npx tsc --noEmit` -> `rtk tsc --noEmit`
- `npm run lint` -> `rtk npm run lint`
- `npm run test` -> `rtk npm run test`
- `npm run build` -> `rtk npm run build`
- `pytest` -> `rtk pytest`
- `cargo test` -> `rtk cargo test`
- `go test` -> `rtk go test`
- `docker logs <container>` -> `rtk docker logs <container>`
- `kubectl logs <pod>` -> `rtk kubectl logs <pod>`

## Allowed Raw Commands

Use the original command directly when any of these are true:
  
1. The output is intentionally tiny, such as `pwd`, `echo`, `which`, `date`, or a single-value check.
2. The command needs exact raw formatting for a follow-up tool, parser, or approval flow.
3. RTK does not support the command, or `rtk proxy <cmd>` is the only safe RTK path.
4. RTK hides details needed for deep debugging after an RTK-first attempt.
5. The repo or tool contract requires the native command shape.

## Repository-Specific Exceptions

- Prefer raw `rg --files` for file enumeration because the result is already compact and exact.
- Prefer raw `sed -n`, `nl -ba`, or `sqlite3` when exact line numbers or SQL output are required.
- Prefer raw `playwright-cli ...` because this repo requires the native CLI and its output is already scoped to a specific browser action.
- Prefer raw short git write commands such as `git add`, `git commit`, and `git push` when you need their exact prompts or confirmations.

## Fallback Rule

- If RTK clearly supports a command, use it first.
- If support is uncertain, try `rtk proxy <command>` before falling back to a raw high-output command.
- If you fall back to the raw command, narrow the scope so the output stays bounded.

## Verification

Run the repo helper after shell-heavy work or when RTK adoption looks suspicious:

```bash
npm run rtk:verify
```

Manual checks:

```bash
which rtk
rtk init --show --codex
npm run rtk:gain -- --history
rtk git status
rtk tsc --noEmit
```

## Troubleshooting

- `rtk gain` can fail in restricted environments even when history exists.
- The default tracking database is usually `~/.local/share/rtk/history.db`.
- Use the repo wrapper to auto-handle sandbox restrictions:

```bash
npm run rtk:gain -- --history
```

- If `rtk gain` cannot open that DB, copy it to `/tmp` and inspect the copy:

```bash
cp ~/.local/share/rtk/history.db /tmp/rtk-history.db
sqlite3 /tmp/rtk-history.db '.tables'
sqlite3 -readonly /tmp/rtk-history.db 'select timestamp, original_cmd, rtk_cmd, saved_tokens from commands order by timestamp desc limit 10;'
```
