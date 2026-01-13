---
name: oask
description: Asynchronously send a task to OpenCode via the `oask` CLI. Use only when the user explicitly delegates to OpenCode (ask/@opencode/let opencode/review); not for questions about OpenCode itself.
metadata:
  short-description: Ask OpenCode asynchronously via oask
  backend: opencode
---

# oask (Ask OpenCode)

Use `oask` to forward the user's request to the OpenCode pane started by `ccb up opencode`.

## Prereqs (Backend)

- `oping` should succeed; otherwise start it with `ccb up opencode`.
- `oask` must run in the same environment as `ccb` (WSL vs native Windows).

## Quick Start

- Preferred (works best on Windows too): `oask "$ARGUMENTS"`
- Multiline (optional): `oask <<'EOF'` … `EOF`

## Workflow (Mandatory)

1. Ensure OpenCode backend is up (`oping`, or run `ccb up opencode`).
2. Run `oask` with the user's request.
3. Reply with a short handoff (e.g. “OpenCode processing: …”) and end the turn; do not poll for results in the same turn.
