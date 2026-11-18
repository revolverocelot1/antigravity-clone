# antigravity-clone

> Complete, instant conversation cloning for Google Antigravity with preserved history, artifacts, pinned status, and timestamps.

`antigravity-clone` provides the `/clone` and `.clone` command for Antigravity. It creates a complete duplicate of your current (or any selected) conversation directly inside the same workspace, keeping every turn, artifact, and timestamp intact while numbering each clone cleanly as `<Title> 1`, `<Title> 2`, and so forth.

---

## Why /clone?

Standard conversation forking in Antigravity has limitations when you just want a backup, checkpoint, or alternative branch:
- **Forking changes timestamps:** Built-in forks stamp the current time (`NOW`) rather than preserving original chat timestamps.
- **Forking changes workspaces:** Forking can separate your conversation context from its current workspace bindings.
- **Pinned status is lost:** Pinned conversations lose their pinned position when forked.
- **Fork markers and tool side-effects:** Native forks inject fork notification steps and can leave database WAL locks uncommitted.

`antigravity-clone` solves this with a dedicated 3-tier cloning engine that runs entirely client-side in Python before calling the LLM—costing **0 tokens** and completing in seconds.

---

## Features

- **Exact Context & Artifact Parity:** Copies all conversation steps, `brain/` directory artifacts, `chapter_titles/`, and workspace/project associations.
- **Timestamp Preservation:** Preserves `createdTime`, `lastUserInputTime`, and `lastUserViewTime` exactly as they were in the original conversation.
- **Pinned Parity:** If the source conversation is pinned in your sidebar, the clone is also pinned at the top.
- **Smart Title Numbering:** Automatically appends ` 1`, ` 2`, ` 3`, etc. without chaining ugly repetitions like ` 1 1`.
- **Zero Token Cost:** Runs as a native `PreInvocation` hook that finishes and outputs an inline status card without consuming model tokens.
- **Idempotency Guard:** Internal execution guard prevents accidental duplicate clones even during rapid retries.
- **3-Tier Update Resilience:**
  1. *Tier 1 (Live Primary):* Uses SQLite online `.backup()` with protobuf wire rewriting and registers the clone live via Antigravity's `LoadTrajectory` and `UpdateConversationAnnotations` RPCs.
  2. *Tier 2 (Fork Fallback):* Automatically falls back to Antigravity's internal `ForkConversation` RPC if disk structures change in future updates.
  3. *Tier 3 (Offline Mode):* Can clone offline directly into SQLite databases and `conversation_summaries.db` when the Antigravity language server is closed.

---

## Quick Install

### Option A: Install via Antigravity Chat (Recommended)
Paste this instruction directly into any Antigravity conversation:

```
Clone https://github.com/revolverocelot1/antigravity-clone into:
- Windows: %USERPROFILE%\.gemini\config\plugins\antigravity-clone
- Mac/Linux: ~/.gemini/config/plugins/antigravity-clone

Then run python test_clone_plugin.py and verify all tests pass.
```

### Option B: PowerShell (Windows)
```powershell
git clone https://github.com/revolverocelot1/antigravity-clone.git "$HOME\.gemini\config\plugins\antigravity-clone"
python -u "$HOME\.gemini\config\plugins\antigravity-clone\test_clone_plugin.py"
```

### Option C: Bash (macOS / Linux)
```bash
git clone https://github.com/revolverocelot1/antigravity-clone.git ~/.gemini/config/plugins/antigravity-clone
python3 -u ~/.gemini/config/plugins/antigravity-clone/test_clone_plugin.py
```

---

## Usage

Type `/clone` or `.clone` in any Antigravity chat input:

| Command | Description |
| :--- | :--- |
| **`/clone`** or **`.clone`** | Clones current conversation as `<Title> 1` with matching timestamps & pinned state. |
| **`/clone My Feature Branch`** | Clones with a custom title. |
| **`/clone --pin`** | Forces the clone to be pinned. |
| **`/clone --unpin`** | Forces the clone to be unpinned. |
| **`/clone --id <uuid>`** | Clones a specific conversation by ID without switching to it. |
| **`/clone --dry-run`** | Previews clone title, step count, and metadata without creating files. |
| **`/clone --keep-trigger`** | Keeps the `/clone` prompt in the cloned conversation history. |
| **`/clone --help`** | Displays inline help and options. |

### CLI Usage
You can also clone conversations directly from your terminal:

```bash
# Clone a conversation by ID
python clone_engine.py --source <CONVERSATION_ID>

# Clone with custom title and force pin
python clone_engine.py --source <CONVERSATION_ID> --name "Checkpoint Alpha" --pin

# Output JSON result for scripting
python clone_engine.py --source <CONVERSATION_ID> --json
```

---

## Verification & Testing

The repository includes a comprehensive 11-point test suite covering wire serialization, title numbering, command parsing, offline sandbox cloning, and live integration:

```bash
python -u test_clone_plugin.py
```

All tests run with zero external pip dependencies (uses Python standard library only).

---

## Repository Structure

```
antigravity-clone/
├── plugin.json             # Antigravity plugin manifest
├── hooks.json              # PreInvocation lifecycle hook registration
├── clone_pre.py            # Hook entry point with execution guard & command parsing
├── clone_engine.py         # 3-tier cloning engine (SQLite, RPC, and offline)
├── wire_pb.py              # Zero-dependency protobuf wire parser & re-encoder
├── test_clone_plugin.py    # 11 unit & integration test cases
├── install.py              # Cross-platform Python installer
├── install.ps1             # PowerShell installer
├── install.sh              # Bash installer
├── skills/
│   └── clone/
│       └── SKILL.md        # Antigravity skill definition for slash menu autocomplete
└── README.md               # Documentation
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
