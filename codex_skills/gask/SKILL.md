---
name: gask
description: Asynchronously send a task to Gemini via the `gask` CLI. Use only when the user explicitly delegates to Gemini (ask/@gemini/let gemini/review); not for questions about Gemini itself.
metadata:
  short-description: Ask Gemini asynchronously via gask
  backend: gemini
---

# gask (Ask Gemini)

Use `gask` to forward the user's request to the Gemini pane started by `ccb up gemini`.

## Prereqs (Backend)

- `gping` should succeed; otherwise start it with `ccb up gemini`.
- `gask` must run in the same environment as `ccb` (WSL vs native Windows).

## Quick Start

- Preferred (works best on Windows too): `gask "$ARGUMENTS"`
- Multiline (optional): `gask <<'EOF'` … `EOF`

## Workflow (Mandatory)

1. Ensure Gemini backend is up (`gping`, or run `ccb up gemini`).
2. Run `gask` with the user's request.
3. Reply with a short handoff (e.g. “Gemini processing: …”) and end the turn; do not poll for results in the same turn.
