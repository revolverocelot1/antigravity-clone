---
name: clone
description: Clone the current (or a specified) Antigravity conversation with all context, steps, artifacts, workspace/project association, pinned state, and timestamps preserved, named `<Title> 1`.
---

# /clone — Conversation Cloner

Creates an independent, standalone clone of an Antigravity conversation without leaving the current workspace.

## Automatic PreInvocation Fast-Path

Typing `/clone` or `.clone` at the start of a chat prompt is intercepted cleanly by the plugin's `PreInvocation` hook (`clone_pre.py`) before any model call:

- `/clone` or `.clone` — Clone the current conversation (`<Title> 1`, `<Title> 2`, ...) in the same workspace/project with the same timestamps and pinned state.
- `/clone My Custom Title` or `/clone --name "My Custom Title"` — Clone with an explicit title.
- `/clone --pin` / `/clone --unpin` — Force pinned state on the clone.
- `/clone --id <cascade-id>` — Clone a specific conversation ID instead of the current one.
- `/clone --dry-run` — Preview what would be cloned without modifying anything.

## Manual / Natural-Language Invocation

When invoked directly by the assistant in response to a natural-language request (e.g., *"clone this chat"* or *"clone that pinned conversation"*):
1. Only run the engine **once** — if this turn was triggered by `/clone` or `.clone`, do **not** run `clone_engine.py` again unless you first check `GetAllCascadeTrajectories` and confirm no clone was already created.
2. Run:

```bash
python -u "C:\Users\ASUS TUF GAMING\.gemini\config\plugins\antigravity-clone\clone_engine.py" --source "<CONVERSATION_ID>"
```

Pass `--json` for structured output, `--pin` / `--unpin` to override pin state, `--name "<title>"` for a custom title, or `--dry-run` to preview first.
