# Async Ask Pattern (cask/gask/oask)

Use this pattern to delegate work to a partner AI (Codex/Gemini/OpenCode) asynchronously.

## When To Use

Use ONLY when the user explicitly delegates to one of:
- Codex: `@codex`, `ask codex`, `let codex`, “让 codex …”, “请 codex …”
- Gemini: `@gemini`, `ask gemini`, `let gemini`, “让 gemini …”, “请 gemini …”
- OpenCode: `@opencode`, `ask opencode`, `let opencode`, “让 opencode …”, “请 opencode …”

DO NOT use when the user asks questions *about* the tool itself.

## Command Mapping

- Codex → `cask` → status: `ccb status codex`
- Gemini → `gask` → status: `ccb status gemini`
- OpenCode → `oask` → status: `ccb status opencode`

## Execution (MANDATORY)

Always run in background. For arbitrary text (quotes, backticks, multiline), prefer passing the user request via a single-quoted HEREDOC (prevents shell backtick/`$()` expansion):

```bash
Bash(<cask|gask|oask> <<'EOF'
<user request, verbatim>
EOF
, run_in_background=true)
```

For short 1-liners, passing as a quoted argument is OK, but be careful with quotes/backticks:

```bash
Bash(<cask|gask|oask> "one line request", run_in_background=true)
```

## Workflow (IMPORTANT)

1. Submit task to background (partner AI processes elsewhere).
2. Immediately end the current turn. Do not wait.
3. The system will recall you when the background command returns.

## After Execution (MANDATORY)

If Bash succeeds:
- Tell the user: “<Provider> processing...”
- Immediately end your turn.
- Do not check status or do additional work in the same turn.

If Bash fails:
- Report the error output.
- Suggest checking backend status with the mapping above.

## Wrong vs Right

- Risky: `Bash(cask "…")` (may break on quotes/backticks; keep it simple)
- Preferred: `Bash(cask <<'EOF' … EOF, run_in_background=true)` then end turn

## Parameters

Supported by `cask/gask/oask`:
- `--timeout SECONDS` (default 3600)
- `--output FILE` (write reply to FILE)
- `-q/--quiet` (reduce stderr noise; timeout still returns non-zero)
