---
name: auto
description: AutoFlow entry point. Routes to tp (plan) or tr (run).
metadata:
  short-description: AutoFlow router (plan or run)
---

AutoFlow entry point. Use Skill tool to invoke the appropriate workflow:

- If `$ARGUMENTS` is empty or starts with "plan"/"p": invoke `tp` skill
- If `$ARGUMENTS` starts with "run"/"r": invoke `tr` skill
- Otherwise: treat `$ARGUMENTS` as requirement and invoke `tp` skill

Examples:
- `/auto implement user login` → tp with "implement user login"
- `/auto plan: add caching` → tp with "add caching"
- `/auto run` → tr
- `/auto r` → tr
