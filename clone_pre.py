"""PreInvocation lifecycle hook handler for Antigravity `/clone` and `.clone`."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from clone_engine import CloneResult, UUID_RE, clone_conversation

CMD_RE = re.compile(r"^\s*[./]clone(?:[ \t]+([^\r\n]*))?\s*$", re.IGNORECASE)
GUARD_FILE = SCRIPT_DIR / ".clone_guard.json"
GUARD_TTL_SECONDS = 300.0


def ensure_utf8_stdio() -> None:
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def unwrap_user_input(raw: str) -> str:
    text = (raw or "").strip()
    m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Strip trailing ADDITIONAL_METADATA if present without USER_REQUEST wrapper
    text = re.sub(
        r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>",
        "",
        text,
        flags=re.DOTALL,
    ).strip()
    return text


def parse_clone_command(raw_user_input: str) -> Optional[Dict[str, Any]]:
    """Return parsed options dict if input is an explicit `/clone` or `.clone` command, else None."""
    text = unwrap_user_input(raw_user_input)
    if not text:
        return None
    m = CMD_RE.match(text)
    if not m:
        return None

    arg_str = (m.group(1) or "").strip()
    opts: Dict[str, Any] = {
        "raw_command": text,
        "custom_title": None,
        "source_id": None,
        "pin_override": None,
        "keep_archived": False,
        "keep_trigger": False,
        "dry_run": False,
        "help": False,
    }
    if not arg_str:
        return opts

    try:
        tokens = shlex.split(arg_str, posix=True)
    except ValueError:
        tokens = arg_str.split()

    title_parts = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        low = tok.lower()
        if low in ("-h", "--help", "?"):
            opts["help"] = True
        elif low in ("--pin", "-p"):
            opts["pin_override"] = True
        elif low in ("--unpin", "-u"):
            opts["pin_override"] = False
        elif low == "--archived":
            opts["keep_archived"] = True
        elif low == "--keep-trigger":
            opts["keep_trigger"] = True
        elif low == "--dry-run":
            opts["dry_run"] = True
        elif low in ("--name", "--title", "-n", "-t") and i + 1 < len(tokens):
            opts["custom_title"] = tokens[i + 1]
            i += 1
        elif (low.startswith("--name=") or low.startswith("--title=")):
            opts["custom_title"] = tok.split("=", 1)[1]
        elif low in ("--id", "--source") and i + 1 < len(tokens):
            opts["source_id"] = tokens[i + 1]
            i += 1
        elif low.startswith("--id=") or low.startswith("--source="):
            opts["source_id"] = tok.split("=", 1)[1]
        elif UUID_RE.match(tok) and opts["source_id"] is None and not title_parts:
            opts["source_id"] = tok
        else:
            title_parts.append(tok)
        i += 1

    if title_parts and not opts["custom_title"]:
        opts["custom_title"] = " ".join(title_parts).strip()
    return opts


def _build_terminate_response(title: str, markdown_text: str) -> Dict[str, Any]:
    """Return a clean PreInvocation termination response without triggering LS map-scrub panics."""
    # IMPORTANT: Never set HookSystemMessage.metadata (`map<string, string>`) because
    # Antigravity's utils.ScrubInternalFields panics on non-empty map<string, string> fields.
    return {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": markdown_text,
                    "renderInfo": {
                        "title": title,
                        "titlePrefix": "Clone",
                        "markdown": markdown_text,
                    },
                }
            }
        ],
        "terminationBehavior": "terminate",
    }


def _execution_guard_key(data: Dict[str, Any], current_cid: str, target_cid: str, raw_cmd: str) -> str:
    exec_id = str(data.get("executionId") or "").strip()
    if exec_id:
        return f"exec:{current_cid}:{exec_id}"
    init_steps = data.get("initialNumSteps")
    if init_steps is not None:
        return f"step:{current_cid}:{target_cid}:{init_steps}:{raw_cmd}"
    return ""


def _load_guard_entries() -> Dict[str, Dict[str, Any]]:
    if not GUARD_FILE.is_file():
        return {}
    try:
        raw = json.loads(GUARD_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        now = time.time()
        return {
            k: v
            for k, v in raw.items()
            if isinstance(v, dict) and (now - float(v.get("ts") or 0)) <= GUARD_TTL_SECONDS
        }
    except Exception:
        return {}


def _save_guard_entries(entries: Dict[str, Dict[str, Any]]) -> None:
    try:
        tmp = GUARD_FILE.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(entries), encoding="utf-8")
        os.replace(tmp, GUARD_FILE)
    except Exception:
        pass


def _format_help_markdown() -> str:
    return (
        "### `/clone` — Antigravity Conversation Cloner\n"
        "- `/clone` or `.clone` — Clone this conversation cleanly as `<Title> 1` (`<Title> 2`, ...) in the same workspace & pinned state with matching timestamps.\n"
        "- `/clone My Title` or `/clone --name \"My Title\"` — Clone with a custom title.\n"
        "- `/clone --pin` / `/clone --unpin` — Force pinned or unpinned status on the clone.\n"
        "- `/clone --id <cascade-id>` — Clone another conversation by UUID.\n"
        "- `/clone --keep-trigger` — Keep the `/clone` message inside the cloned history.\n"
        "- `/clone --dry-run` — Preview without creating anything."
    )


def _format_result_markdown(res: CloneResult) -> str:
    if not res.ok:
        return f"❌ **Clone Failed:** `{res.error}`"

    badge = "🔍 **Clone Dry-Run Preview**" if res.mode == "dry_run" else "✅ **Conversation Cloned**"
    pin_txt = "Pinned at top 📌" if res.pinned else "No"
    ws_txt = ", ".join(f"`{u}`" for u in res.workspace_uris) if res.workspace_uris else "`Current workspace`"
    lines = [
        f"{badge}: **{res.clone_title}** (`{res.clone_cascade_id}`)",
        f"- **Source:** **{res.source_title}** (`{res.source_cascade_id[:8]}`)",
        f"- **Pinned:** {pin_txt} | **Steps:** {res.step_count} | **Artifacts copied:** {res.brain_files_copied} files",
        f"- **Workspace:** {ws_txt} | **Method:** `{res.mode}` ({res.elapsed_ms} ms)",
        f"- **Timestamps:** created `{res.created_time or 'preserved'}` • updated `{res.last_modified_time or 'preserved'}`",
    ]
    if res.warnings:
        lines.append("- **Notes:** " + "; ".join(res.warnings))
    return "\n".join(lines)


def handle_pre_invocation(data: Dict[str, Any]) -> Dict[str, Any]:
    if data.get("continuationActive"):
        return {"injectSteps": [], "terminationBehavior": ""}

    # Note: invocationNum may be 1-based or 0-based depending on Antigravity build;
    # rely primarily on execution/step idempotency below and still skip obvious >1 loops.
    raw_inv = data.get("invocationNum")
    if raw_inv is not None:
        try:
            if int(raw_inv) > 1:
                return {"injectSteps": [], "terminationBehavior": ""}
        except (TypeError, ValueError):
            pass

    parsed = parse_clone_command(str(data.get("lastUserInput") or ""))
    if parsed is None:
        return {"injectSteps": [], "terminationBehavior": ""}

    if parsed["help"]:
        return _build_terminate_response("Clone Help", _format_help_markdown())

    current_cid = str(
        data.get("conversationId") or os.environ.get("ANTIGRAVITY_CONVERSATION_ID") or ""
    ).strip()
    target_cid = (parsed["source_id"] or current_cid).strip()
    cloning_self = bool(current_cid and target_cid == current_cid)

    guard_key = "" if parsed["dry_run"] else _execution_guard_key(
        data, current_cid, target_cid, parsed["raw_command"]
    )
    if guard_key:
        prior = _load_guard_entries().get(guard_key)
        if prior:
            # Already executed in this execution/step (e.g., invocationNum=1 follow-up on 0-based LS):
            # never create a second clone!
            return {"injectSteps": [], "terminationBehavior": "terminate"}

    initial_steps: Optional[int] = None
    if data.get("initialNumSteps") is not None:
        try:
            initial_steps = int(data["initialNumSteps"])
        except (TypeError, ValueError):
            initial_steps = None

    app_data_dir = os.environ.get("ANTIGRAVITY_APP_DATA_DIR") or None
    if not app_data_dir and data.get("artifactDirectoryPath"):
        # e.g. <appDataDir>/brain/<cid>
        try:
            art = Path(str(data["artifactDirectoryPath"])).resolve()
            if art.parent.name == "brain":
                app_data_dir = str(art.parent.parent)
        except Exception:
            pass

    if guard_key:
        entries = _load_guard_entries()
        entries[guard_key] = {"ts": time.time(), "source": target_cid}
        _save_guard_entries(entries)

    res = clone_conversation(
        target_cid,
        app_data_dir=app_data_dir,
        custom_title=parsed["custom_title"],
        pin_override=parsed["pin_override"],
        keep_archived=parsed["keep_archived"],
        strip_trigger_input=(
            parsed["raw_command"] if (cloning_self and not parsed["keep_trigger"]) else None
        ),
        initial_num_steps=initial_steps if cloning_self else None,
        drop_trailing_running=not parsed["keep_trigger"],
        dry_run=parsed["dry_run"],
    )

    if guard_key:
        entries = _load_guard_entries()
        entries[guard_key] = {
            "ts": time.time(),
            "source": target_cid,
            "clone_id": res.clone_cascade_id,
            "ok": res.ok,
        }
        _save_guard_entries(entries)

    if not res.ok:
        return {
            "injectSteps": [
                {"errorMessage": {"userMessage": f"Clone failed: {res.error}"}}
            ],
            "terminationBehavior": "terminate",
        }

    title = "Clone Dry-Run Preview" if res.mode == "dry_run" else f"Cloned: {res.clone_title}"
    return _build_terminate_response(title, _format_result_markdown(res))


def main() -> None:
    ensure_utf8_stdio()
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        result = handle_pre_invocation(data)
    except Exception as exc:
        result = {
            "injectSteps": [
                {"errorMessage": {"userMessage": f"/clone Hook Error: {exc}"}}
            ],
            "terminationBehavior": "terminate",
        }
    json.dump(result, sys.stdout, ensure_ascii=True)


if __name__ == "__main__":
    main()
