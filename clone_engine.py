"""Core conversation cloning engine for the antigravity-clone plugin.

Supports three resilient execution tiers:
  1. Online SQLite backup + wire-safe metadata update + LoadTrajectory + UpdateConversationAnnotations
     (Preserves exact timestamps, clean history, workspace/project bindings, pinned state, and live UI.)
  2. Online native ForkConversation RPC + UpdateConversationAnnotations fallback
     (Future-proof fallback if Antigravity changes local SQLite layout.)
  3. Offline SQLite + annotations + conversation_summaries.db clone
     (Works cleanly even when Antigravity / language_server is not running.)
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import socket
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import wire_pb

# CortexStepType & CortexStepStatus constants observed in Antigravity hub-2.32.0
STEP_TYPE_USER_INPUT = 14
STEP_STATUS_UNSPECIFIED = 0
STEP_STATUS_PENDING = 1
STEP_STATUS_RUNNING = 2
STEP_STATUS_DONE = 3
STEP_STATUS_CANCELED = 6
STEP_STATUS_ERROR = 7
TERMINAL_STEP_STATUSES = {STEP_STATUS_DONE, STEP_STATUS_CANCELED, STEP_STATUS_ERROR}

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass
class CloneResult:
    ok: bool
    mode: str  # "load_trajectory" | "fork_fallback" | "offline" | "dry_run"
    source_cascade_id: str
    clone_cascade_id: str
    source_title: str
    clone_title: str
    pinned: bool
    project_id: str
    workspace_uris: List[str] = field(default_factory=list)
    created_time: str = ""
    last_modified_time: str = ""
    last_user_input_time: str = ""
    step_count: int = 0
    brain_files_copied: int = 0
    elapsed_ms: int = 0
    warnings: List[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def get_app_data_dir(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_dir = os.environ.get("ANTIGRAVITY_APP_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return (Path.home() / ".gemini" / "antigravity").resolve()


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)
            ):
                return exit_code.value == STILL_ACTIVE
            return False
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _tcp_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


class LanguageServerClient:
    """Thin ConnectRPC JSON client for Antigravity's LanguageServerService."""

    def __init__(self, base_url: str, csrf_token: str, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.csrf_token = csrf_token
        self.timeout = timeout
        self._ssl_ctx = ssl._create_unverified_context()

    def call(
        self, method: str, payload: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/exa.language_server_pb.LanguageServerService/{method}"
        body = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-codeium-csrf-token": self.csrf_token,
            },
            method="POST",
        )
        kwargs: Dict[str, Any] = {"timeout": timeout or self.timeout}
        if url.startswith("https://"):
            kwargs["context"] = self._ssl_ctx
        with urllib.request.urlopen(req, **kwargs) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}


def _same_dir(a: Path, b: str) -> bool:
    if not b:
        return True
    try:
        return os.path.normcase(str(a.resolve())) == os.path.normcase(str(Path(b).expanduser().resolve()))
    except Exception:
        return False


def discover_live_ls(
    app_data_dir: Path,
    source_cascade_id: Optional[str] = None,
) -> Tuple[Optional[LanguageServerClient], Dict[str, Dict[str, Any]]]:
    """Find a live LanguageServerService instance (preferring env vars and one hosting `source_cascade_id`)."""
    candidates: List[Tuple[str, str]] = []

    env_app_dir = os.environ.get("ANTIGRAVITY_APP_DATA_DIR", "").strip()
    use_env_ls = (not env_app_dir) or _same_dir(app_data_dir, env_app_dir)
    if not use_env_ls and not (app_data_dir / "daemon").is_dir():
        return None, {}

    if use_env_ls:
        env_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
        env_csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "").strip()
        if env_addr and env_csrf:
            base = env_addr if env_addr.startswith(("http://", "https://")) else f"http://{env_addr}"
            candidates.append((base, env_csrf))
            # Fast path: try current process LS first before scanning daemon/*.json
            fast_client = LanguageServerClient(base, env_csrf, timeout=15.0)
            try:
                res = fast_client.call("GetAllCascadeTrajectories", {}, timeout=5.0)
                summaries = res.get("trajectorySummaries") or {}
                if source_cascade_id is None or source_cascade_id in summaries:
                    return fast_client, summaries
                first_live = (fast_client, summaries)
            except Exception:
                first_live = None
        else:
            first_live = None
    else:
        first_live = None

    seen: Set[Tuple[str, str]] = {c for c in candidates}
    daemon_dir = app_data_dir / "daemon"
    if daemon_dir.is_dir():
        hub_files = sorted(
            daemon_dir.glob("hub_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for hf in hub_files:
            try:
                info = json.loads(hf.read_text(encoding="utf-8"))
            except Exception:
                continue
            pid = int(info.get("pid") or 0)
            token = str(info.get("csrfToken") or "").strip()
            http_port = info.get("httpPort")
            https_port = info.get("httpsPort")
            if not token:
                continue
            if pid and not _is_pid_alive(pid):
                continue
            for scheme, port in (("http", http_port), ("https", https_port)):
                if not port or not _tcp_open("127.0.0.1", int(port)):
                    continue
                key = (f"{scheme}://127.0.0.1:{int(port)}", token)
                if key in seen:
                    continue
                seen.add(key)
                client = LanguageServerClient(key[0], token, timeout=15.0)
                try:
                    res = client.call("GetAllCascadeTrajectories", {}, timeout=5.0)
                except Exception:
                    continue
                summaries = res.get("trajectorySummaries") or {}
                if source_cascade_id and source_cascade_id in summaries:
                    return client, summaries
                if first_live is None:
                    first_live = (client, summaries)
                break

    if first_live is not None:
        return first_live
    return None, {}


def _unescape_pbtxt_str(raw: str) -> str:
    # Handle basic C/prototext escapes cleanly
    out: List[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "r":
                out.append("\r")
                i += 2
                continue
            if nxt == "t":
                out.append("\t")
                i += 2
                continue
            if nxt in ('"', "\\", "'"):
                out.append(nxt)
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _escape_pbtxt_str(val: str) -> str:
    return (
        val.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def parse_pbtxt_annotations(pbtxt_path: Path) -> Dict[str, Any]:
    """Extract key annotations fields from `annotations/<cid>.pbtxt` for offline/pre-load parity."""
    if not pbtxt_path.is_file():
        return {}
    text = pbtxt_path.read_text(encoding="utf-8", errors="replace")
    parsed: Dict[str, Any] = {}
    m_title = re.search(r'(?:^|\s)title\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if m_title:
        parsed["title"] = _unescape_pbtxt_str(m_title.group(1))
    m_pinned = re.search(r"(?:^|\s)pinned\s*:\s*(true|false)\b", text)
    if m_pinned:
        parsed["pinned"] = m_pinned.group(1) == "true"
    m_starred = re.search(r"(?:^|\s)starred\s*:\s*(true|false)\b", text)
    if m_starred:
        parsed["starred"] = m_starred.group(1) == "true"
    m_archived = re.search(r"(?:^|\s)archived\s*:\s*(true|false)\b", text)
    if m_archived:
        parsed["archived"] = m_archived.group(1) == "true"
    return parsed


def build_pbtxt_for_clone(
    src_pbtxt: Path,
    clone_title: str,
    pinned: bool,
    keep_archived: bool = False,
) -> str:
    """Build valid prototext annotations for `annotations/<new_cid>.pbtxt`."""
    raw = src_pbtxt.read_text(encoding="utf-8", errors="replace") if src_pbtxt.is_file() else ""
    cleaned = raw
    # Strip fields we explicitly manage or do not want leaked onto clone
    for scalar_key in (
        "title",
        "pinned",
        "archived",
        "marked_as_unread",
        "side_question_cascade_ids",
        "side_question_titles",
    ):
        cleaned = re.sub(
            rf'(?:^|\s){scalar_key}\s*:\s*(?:"(?:[^"\\]|\\.)*"|true|false|-?\d+)\s*',
            " ",
            cleaned,
        )
    if not keep_archived:
        cleaned = re.sub(
            r"(?:^|\s)archival_status_timestamp\s*:\s*\{[^}]*\}\s*",
            " ",
            cleaned,
        )
    parts: List[str] = [f'title:"{_escape_pbtxt_str(clone_title)}"']
    if cleaned.strip():
        parts.append(cleaned.strip())
    if pinned:
        parts.append("pinned:true")
    return " ".join(parts).strip() + "\n"


def extract_display_title(
    summary: Optional[Dict[str, Any]],
    pbtxt_parsed: Optional[Dict[str, Any]] = None,
) -> str:
    """Match Antigravity UI `vt(summary)` title precedence."""
    if summary:
        ann = summary.get("annotations") or {}
        t = (ann.get("title") or "").strip()
        if t:
            return t
        s = (summary.get("summary") or "").strip()
        if s:
            return s
    if pbtxt_parsed:
        t = (pbtxt_parsed.get("title") or "").strip()
        if t:
            return t
    return "Untitled Conversation"


def collect_existing_titles(
    summaries: Dict[str, Dict[str, Any]],
    annotations_dir: Path,
) -> Set[str]:
    titles: Set[str] = set()
    for s in summaries.values():
        ann = s.get("annotations") or {}
        if ann.get("title"):
            titles.add(str(ann["title"]).strip())
        if s.get("summary"):
            titles.add(str(s["summary"]).strip())
    if annotations_dir.is_dir():
        for p in annotations_dir.glob("*.pbtxt"):
            parsed = parse_pbtxt_annotations(p)
            if parsed.get("title"):
                titles.add(parsed["title"].strip())
    return titles


def compute_clone_title(
    base_title: str,
    existing_titles: Set[str],
    custom_title: Optional[str] = None,
) -> str:
    """Compute `<Title> 1` (or `<Title> 2`, ... if already taken), preserving user override."""
    if custom_title and custom_title.strip():
        return custom_title.strip()

    clean_base = (base_title or "Untitled Conversation").strip() or "Untitled Conversation"
    stem = clean_base
    start_num = 1

    # If user is cloning an existing clone like `"Chat 1"` and `"Chat"` exists, increment from stem `"Chat"`
    m = re.match(r"^(.*\S)\s+(\d{1,3})$", clean_base)
    if m:
        maybe_stem = m.group(1).strip()
        maybe_num = int(m.group(2))
        if maybe_stem in existing_titles and maybe_num >= 1:
            stem = maybe_stem
            start_num = maybe_num + 1

    n = start_num
    while True:
        candidate = f"{stem} {n}"
        if candidate not in existing_titles:
            return candidate
        n += 1


def _sqlite_connect_ro(db_path: Path, timeout: float = 15.0) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=timeout)


def _sqlite_backup_with_retry(src_conn: sqlite3.Connection, dst_conn: sqlite3.Connection) -> None:
    last_err: Optional[Exception] = None
    for attempt in range(5):
        try:
            src_conn.backup(dst_conn, pages=2000)
            return
        except sqlite3.OperationalError as exc:
            last_err = exc
            time.sleep(0.15 * (attempt + 1))
    if last_err:
        raise last_err


def _find_clone_trigger_cut_idx(
    conn: sqlite3.Connection,
    strip_trigger_input: Optional[str],
    initial_num_steps: Optional[int],
) -> Optional[int]:
    """Identify the index of an active `/clone` prompt step at the tail so the clone omits `/clone`."""
    if not strip_trigger_input and initial_num_steps is None:
        return None
    tail = conn.execute(
        "SELECT idx, step_type, status, step_format, step_payload FROM steps ORDER BY idx DESC LIMIT 10"
    ).fetchall()
    needle_bytes: Optional[bytes] = None
    if strip_trigger_input:
        trimmed = strip_trigger_input.strip()
        if trimmed:
            needle_bytes = trimmed.encode("utf-8", errors="ignore")[:120]

    for idx, step_type, status, step_format, payload in tail:
        if step_type == STEP_TYPE_USER_INPUT:
            raw = bytes(payload or b"")
            if needle_bytes and needle_bytes in raw:
                return int(idx)
            if (
                initial_num_steps is not None
                and int(idx) >= max(0, int(initial_num_steps) - 1)
                and (b"/clone" in raw or b".clone" in raw)
            ):
                return int(idx)
            # Only inspect up to the most recent USER_INPUT
            break
    return None


def _prepare_cloned_sqlite_db(
    src_db: Path,
    dst_db: Path,
    src_cid: str,
    new_cid: str,
    new_traj_id: str,
    *,
    strip_trigger_input: Optional[str] = None,
    initial_num_steps: Optional[int] = None,
    drop_trailing_running: bool = True,
) -> int:
    """Copy `src_db` cleanly via SQLite backup and rewrite identifiers/non-terminal steps.

    Returns final step count in the cloned database.
    """
    tmp_db = dst_db.with_suffix(".db.tmp")
    for p in (
        tmp_db,
        Path(str(tmp_db) + "-wal"),
        Path(str(tmp_db) + "-shm"),
        dst_db,
        Path(str(dst_db) + "-wal"),
        Path(str(dst_db) + "-shm"),
    ):
        if p.exists():
            p.unlink()

    sconn = _sqlite_connect_ro(src_db, timeout=15.0)
    try:
        dconn = sqlite3.connect(str(tmp_db), timeout=15.0)
        try:
            _sqlite_backup_with_retry(sconn, dconn)
        finally:
            sconn.close()

        tables = {
            r[0]
            for r in dconn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        if "trajectory_meta" in tables:
            cols = {
                r[1]
                for r in dconn.execute("PRAGMA table_info(trajectory_meta)").fetchall()
            }
            if {"cascade_id", "trajectory_id"}.issubset(cols):
                dconn.execute(
                    "UPDATE trajectory_meta SET cascade_id=?, trajectory_id=?",
                    (new_cid, new_traj_id),
                )

        if "parent_references" in tables:
            # Prevent future DeleteCascadeTrajectory(clone) from affecting source's subagents
            dconn.execute("DELETE FROM parent_references")

        if "trajectory_metadata_blob" in tables:
            rows = dconn.execute(
                "SELECT id, data FROM trajectory_metadata_blob"
            ).fetchall()
            for row_id, data in rows:
                if not data:
                    continue
                try:
                    updated = wire_pb.rewrite_string_field(
                        bytes(data),
                        6,
                        new_cid,
                        only_if_matches=src_cid,
                        add_if_missing=False,
                    )
                    if updated == bytes(data):
                        # If field 6 existed with another value, keep behavior safe when row_id=='main'
                        if row_id == "main":
                            updated = wire_pb.rewrite_string_field(
                                bytes(data), 6, new_cid, add_if_missing=False
                            )
                    dconn.execute(
                        "UPDATE trajectory_metadata_blob SET data=? WHERE id=?",
                        (updated, row_id),
                    )
                except ValueError:
                    pass

        if "steps" in tables:
            # 1. Optionally trim `/clone` invocation trigger step
            cut_idx = _find_clone_trigger_cut_idx(
                dconn, strip_trigger_input, initial_num_steps
            )
            if cut_idx is not None:
                dconn.execute("DELETE FROM steps WHERE idx >= ?", (cut_idx,))

            # 2. Trim trailing active RUNNING/PENDING steps (e.g. an in-flight tool call)
            if drop_trailing_running:
                while True:
                    last_row = dconn.execute(
                        "SELECT idx, status FROM steps ORDER BY idx DESC LIMIT 1"
                    ).fetchone()
                    if last_row and last_row[1] not in TERMINAL_STEP_STATUSES:
                        dconn.execute("DELETE FROM steps WHERE idx=?", (last_row[0],))
                    else:
                        break

            # 3. Normalize any remaining non-terminal steps in the middle so LoadTrajectory opens IDLE
            non_term = dconn.execute(
                "SELECT idx, status, step_format, step_payload FROM steps WHERE status NOT IN (3, 6, 7)"
            ).fetchall()
            for idx, _st, sfmt, payload in non_term:
                new_payload = payload
                if payload and sfmt == 0:
                    try:
                        new_payload = wire_pb.rewrite_varint_field(
                            bytes(payload), 4, STEP_STATUS_CANCELED, add_if_missing=False
                        )
                    except ValueError:
                        pass
                dconn.execute(
                    "UPDATE steps SET status=?, step_payload=? WHERE idx=?",
                    (STEP_STATUS_CANCELED, new_payload, idx),
                )

            step_count = int(
                dconn.execute("SELECT COUNT(*) FROM steps").fetchone()[0]
            )
        else:
            step_count = 0

        # IMPORTANT: commit before closing/changing journal_mode!
        dconn.commit()
        try:
            dconn.execute("PRAGMA journal_mode=DELETE")
            dconn.commit()
        except sqlite3.OperationalError:
            pass
        dconn.close()

        os.replace(str(tmp_db), str(dst_db))
        return step_count
    except Exception:
        for p in (
            tmp_db,
            Path(str(tmp_db) + "-wal"),
            Path(str(tmp_db) + "-shm"),
        ):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        raise


def _copy_brain_dir(src_dir: Path, dst_dir: Path) -> Tuple[int, List[str]]:
    """Copy `brain/<src_cid>` to `brain/<new_cid>` tolerantly."""
    if not src_dir.is_dir():
        return 0, []
    copied = 0
    warnings: List[str] = []
    dst_dir.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(src_dir):
        rel = os.path.relpath(root, src_dir)
        target_root = dst_dir if rel == "." else dst_dir / rel
        target_root.mkdir(parents=True, exist_ok=True)
        for d in dirs:
            (target_root / d).mkdir(exist_ok=True)
        for fn in files:
            src_f = Path(root) / fn
            dst_f = target_root / fn
            try:
                if src_f.is_symlink():
                    link_target = os.readlink(src_f)
                    if dst_f.exists() or dst_f.is_symlink():
                        dst_f.unlink()
                    os.symlink(link_target, dst_f)
                else:
                    shutil.copy2(src_f, dst_f)
                copied += 1
            except OSError as exc:
                warnings.append(f"Skipped locked/unreadable brain file {rel}/{fn}: {exc}")
    return copied, warnings


def _iso_to_sql_utc(iso_str: str) -> str:
    """Convert RFC3339 timestamp to SQLite conversation_summaries format or return original."""
    if not iso_str:
        return ""
    try:
        # Handle up to 9 fractional digits cleanly by trimming to 6
        s = iso_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$", s)
        if m:
            frac = (m.group(2) or "0").ljust(6, "0")[:6]
            dt = datetime.fromisoformat(f"{m.group(1)}.{frac}{m.group(3)}")
            dt_utc = dt.astimezone(timezone.utc)
            frac_full = m.group(2) or ""
            base = dt_utc.strftime("%Y-%m-%d %H:%M:%S")
            if frac_full:
                base += "." + frac_full.rstrip("0") if frac_full.rstrip("0") else ""
            return base + "+00:00"
    except Exception:
        pass
    return iso_str


def _build_rpc_annotations(
    src_summary: Optional[Dict[str, Any]],
    pbtxt_parsed: Dict[str, Any],
    clone_title: str,
    pin_override: Optional[bool],
    keep_archived: bool,
) -> Dict[str, Any]:
    src_ann = dict((src_summary or {}).get("annotations") or {})
    out: Dict[str, Any] = {}
    for key in (
        "tags",
        "starred",
        "lastUserViewTime",
        "groupId",
        "groupIndex",
        "groupDescription",
        "cls",
        "activeProfile",
        "conversationGroupId",
        "sensitivityLevel",
        "permissionPreset",
        "coordinator",
    ):
        if key in src_ann and src_ann[key] not in (None, "", []):
            out[key] = src_ann[key]
    if keep_archived and src_ann.get("archived"):
        out["archived"] = True
        if src_ann.get("archivalStatusTimestamp"):
            out["archivalStatusTimestamp"] = src_ann["archivalStatusTimestamp"]

    src_pinned = bool(src_ann.get("pinned", pbtxt_parsed.get("pinned", False)))
    final_pinned = src_pinned if pin_override is None else bool(pin_override)
    if final_pinned:
        out["pinned"] = True
    out["title"] = clone_title
    return out


def _offline_clone_summary_row(
    app_data_dir: Path,
    src_cid: str,
    new_cid: str,
    new_traj_id: str,
    clone_title: str,
    step_count: int,
    final_pinned: bool,
) -> None:
    """Fallback when LS is offline: duplicate row in `conversation_summaries.db` cleanly."""
    sdb = app_data_dir / "conversation_summaries.db"
    if not sdb.is_file():
        return
    conn = sqlite3.connect(str(sdb), timeout=15.0)
    try:
        cols = [
            r[1]
            for r in conn.execute("PRAGMA table_info(conversation_summaries)").fetchall()
        ]
        if not cols:
            return
        quoted_cols = ", ".join(f"[{c}]" for c in cols)
        row = conn.execute(
            f"SELECT {quoted_cols} FROM conversation_summaries WHERE conversation_id=?",
            (src_cid,),
        ).fetchone()
        if not row:
            return
        row_map = dict(zip(cols, row))
        row_map["conversation_id"] = new_cid
        row_map["title"] = clone_title
        row_map["status"] = "CASCADE_RUN_STATUS_IDLE"
        if "not_fully_idle" in row_map:
            row_map["not_fully_idle"] = 0
        if "killed" in row_map:
            row_map["killed"] = 0
        if step_count > 0:
            row_map["step_count"] = step_count
        raw = row_map.get("raw_summary")
        if raw:
            try:
                b = bytes(raw)
                b = wire_pb.rewrite_string_field(b, 1, clone_title, add_if_missing=True)
                b = wire_pb.rewrite_string_field(b, 4, new_traj_id, add_if_missing=False)
                b = wire_pb.rewrite_varint_field(b, 5, 1, add_if_missing=False)  # IDLE=1
                def _patch_ann(ab: bytes) -> bytes:
                    ab2 = wire_pb.rewrite_string_field(ab, 1, clone_title, add_if_missing=True)
                    if final_pinned:
                        ab2 = wire_pb.rewrite_varint_field(ab2, 12, 1, add_if_missing=True)
                    else:
                        ab2 = wire_pb.remove_field(ab2, 12)
                    ab2 = wire_pb.remove_field(ab2, 4)  # archived
                    ab2 = wire_pb.remove_field(ab2, 8)  # marked_as_unread
                    return ab2

                b = wire_pb.upsert_bytes_field(b, 15, _patch_ann)
                b = wire_pb.rewrite_bytes_field(
                    b, 17, lambda tm: wire_pb.rewrite_string_field(tm, 6, new_cid, add_if_missing=False)
                )
                row_map["raw_summary"] = b
            except ValueError:
                pass
        placeholders = ", ".join(["?"] * len(cols))
        conn.execute(
            f"INSERT OR REPLACE INTO conversation_summaries ({quoted_cols}) VALUES ({placeholders})",
            [row_map[c] for c in cols],
        )
        conn.commit()
    finally:
        conn.close()


def _cleanup_partial_clone(
    app_data_dir: Path,
    new_cid: str,
    client: Optional[LanguageServerClient] = None,
) -> None:
    if client is not None:
        try:
            client.call("DeleteCascadeTrajectory", {"cascadeId": new_cid}, timeout=8.0)
        except Exception:
            pass
    for sub, suffixes in (
        ("conversations", (".db", ".db-wal", ".db-shm", ".db.tmp", ".pb")),
        ("annotations", (".pbtxt",)),
        ("chapter_titles", (".pb",)),
    ):
        for suf in suffixes:
            p = app_data_dir / sub / f"{new_cid}{suf}"
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
    bdir = app_data_dir / "brain" / new_cid
    if bdir.is_dir():
        shutil.rmtree(bdir, ignore_errors=True)


def clone_conversation(
    source_cascade_id: str,
    *,
    app_data_dir: Optional[str] = None,
    custom_title: Optional[str] = None,
    pin_override: Optional[bool] = None,
    keep_archived: bool = False,
    strip_trigger_input: Optional[str] = None,
    initial_num_steps: Optional[int] = None,
    drop_trailing_running: bool = True,
    dry_run: bool = False,
) -> CloneResult:
    """Clone `source_cascade_id` into a brand-new conversation with full fidelity."""
    t0 = time.monotonic()
    src_cid = (source_cascade_id or "").strip()
    if not UUID_RE.match(src_cid):
        return CloneResult(
            ok=False,
            mode="error",
            source_cascade_id=src_cid,
            clone_cascade_id="",
            source_title="",
            clone_title="",
            pinned=False,
            project_id="",
            error=f"Invalid source conversation ID: {src_cid!r}",
        )

    root_dir = get_app_data_dir(app_data_dir)
    client, summaries = discover_live_ls(root_dir, src_cid)
    src_summary = summaries.get(src_cid)
    src_pbtxt = root_dir / "annotations" / f"{src_cid}.pbtxt"
    pbtxt_parsed = parse_pbtxt_annotations(src_pbtxt)

    src_db = root_dir / "conversations" / f"{src_cid}.db"
    src_pb = root_dir / "conversations" / f"{src_cid}.pb"
    if not src_db.is_file() and not src_pb.is_file() and src_summary is None:
        return CloneResult(
            ok=False,
            mode="error",
            source_cascade_id=src_cid,
            clone_cascade_id="",
            source_title="",
            clone_title="",
            pinned=False,
            project_id="",
            error=f"Conversation {src_cid} not found on disk or in live LanguageServer.",
        )

    src_title = extract_display_title(src_summary, pbtxt_parsed)
    existing_titles = collect_existing_titles(summaries, root_dir / "annotations")
    clone_title = compute_clone_title(src_title, existing_titles, custom_title)
    rpc_annotations = _build_rpc_annotations(
        src_summary, pbtxt_parsed, clone_title, pin_override, keep_archived
    )
    final_pinned = bool(rpc_annotations.get("pinned", False))

    tm = (src_summary or {}).get("trajectoryMetadata") or {}
    project_id = str(tm.get("projectId") or "")
    workspace_uris = list(tm.get("workspaceUris") or [])
    if not workspace_uris:
        for w in (src_summary or {}).get("workspaces") or []:
            u = w.get("workspaceFolderAbsoluteUri")
            if u:
                workspace_uris.append(u)

    new_cid = str(uuid.uuid4())
    new_traj_id = str(uuid.uuid4())
    warnings: List[str] = []

    if dry_run:
        return CloneResult(
            ok=True,
            mode="dry_run",
            source_cascade_id=src_cid,
            clone_cascade_id=new_cid,
            source_title=src_title,
            clone_title=clone_title,
            pinned=final_pinned,
            project_id=project_id,
            workspace_uris=workspace_uris,
            created_time=str((src_summary or {}).get("createdTime") or ""),
            last_modified_time=str((src_summary or {}).get("lastModifiedTime") or ""),
            last_user_input_time=str((src_summary or {}).get("lastUserInputTime") or ""),
            step_count=int((src_summary or {}).get("stepCount") or 0),
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    # --- TIER 1 / TIER 3: Disk clone + LoadTrajectory (preferred: exact timestamps!) ---
    has_disk_store = src_db.is_file() or src_pb.is_file()
    if has_disk_store:
        try:
            (root_dir / "conversations").mkdir(parents=True, exist_ok=True)
            (root_dir / "annotations").mkdir(parents=True, exist_ok=True)

            if src_db.is_file():
                dst_db = root_dir / "conversations" / f"{new_cid}.db"
                step_count = _prepare_cloned_sqlite_db(
                    src_db,
                    dst_db,
                    src_cid,
                    new_cid,
                    new_traj_id,
                    strip_trigger_input=strip_trigger_input,
                    initial_num_steps=initial_num_steps,
                    drop_trailing_running=drop_trailing_running,
                )
            else:
                dst_pb = root_dir / "conversations" / f"{new_cid}.pb"
                shutil.copy2(src_pb, dst_pb)
                step_count = int((src_summary or {}).get("stepCount") or 0)

            dst_pbtxt = root_dir / "annotations" / f"{new_cid}.pbtxt"
            dst_pbtxt.write_text(
                build_pbtxt_for_clone(
                    src_pbtxt, clone_title, final_pinned, keep_archived=keep_archived
                ),
                encoding="utf-8",
            )

            src_ct = root_dir / "chapter_titles" / f"{src_cid}.pb"
            if src_ct.is_file():
                (root_dir / "chapter_titles").mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_ct, root_dir / "chapter_titles" / f"{new_cid}.pb")

            brain_copied, brain_warnings = _copy_brain_dir(
                root_dir / "brain" / src_cid, root_dir / "brain" / new_cid
            )
            warnings.extend(brain_warnings)

            if client is not None:
                # Register clone live into running Antigravity UI & summaries DB
                client.call("LoadTrajectory", {"cascadeId": new_cid}, timeout=90.0)
                client.call(
                    "UpdateConversationAnnotations",
                    {
                        "cascadeId": new_cid,
                        "annotations": rpc_annotations,
                        "mergeAnnotations": False,
                    },
                    timeout=15.0,
                )
                refreshed = client.call("GetAllCascadeTrajectories", {}, timeout=15.0)
                clone_sum = (refreshed.get("trajectorySummaries") or {}).get(new_cid)
                if not clone_sum:
                    raise RuntimeError("LoadTrajectory returned 200 but clone summary missing")

                ctm = clone_sum.get("trajectoryMetadata") or {}
                curis = list(ctm.get("workspaceUris") or workspace_uris)
                return CloneResult(
                    ok=True,
                    mode="load_trajectory",
                    source_cascade_id=src_cid,
                    clone_cascade_id=new_cid,
                    source_title=src_title,
                    clone_title=clone_title,
                    pinned=bool((clone_sum.get("annotations") or {}).get("pinned", final_pinned)),
                    project_id=str(ctm.get("projectId") or project_id),
                    workspace_uris=curis,
                    created_time=str(clone_sum.get("createdTime") or ""),
                    last_modified_time=str(clone_sum.get("lastModifiedTime") or ""),
                    last_user_input_time=str(clone_sum.get("lastUserInputTime") or ""),
                    step_count=int(clone_sum.get("stepCount") or step_count),
                    brain_files_copied=brain_copied,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    warnings=warnings,
                )

            # Tier 3: Antigravity offline mode
            _offline_clone_summary_row(
                root_dir, src_cid, new_cid, new_traj_id, clone_title, step_count, final_pinned
            )
            return CloneResult(
                ok=True,
                mode="offline",
                source_cascade_id=src_cid,
                clone_cascade_id=new_cid,
                source_title=src_title,
                clone_title=clone_title,
                pinned=final_pinned,
                project_id=project_id,
                workspace_uris=workspace_uris,
                created_time=str((src_summary or {}).get("createdTime") or ""),
                last_modified_time=str((src_summary or {}).get("lastModifiedTime") or ""),
                last_user_input_time=str((src_summary or {}).get("lastUserInputTime") or ""),
                step_count=step_count,
                brain_files_copied=brain_copied,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                warnings=warnings
                + ["LanguageServer not reachable; created offline clone cleanly for next launch."],
            )
        except Exception as tier1_exc:
            _cleanup_partial_clone(root_dir, new_cid, client=client)
            if client is None:
                return CloneResult(
                    ok=False,
                    mode="error",
                    source_cascade_id=src_cid,
                    clone_cascade_id="",
                    source_title=src_title,
                    clone_title=clone_title,
                    pinned=final_pinned,
                    project_id=project_id,
                    workspace_uris=workspace_uris,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    error=f"Offline clone failed: {tier1_exc}",
                )
            warnings.append(f"Primary LoadTrajectory clone fell back to ForkConversation: {tier1_exc}")

    # --- TIER 2: Native ForkConversation fallback (resilient across storage format changes) ---
    if client is not None:
        fork_cid = ""
        try:
            fork_idx = max(0, int((src_summary or {}).get("stepCount") or 1) - 1)
            fres = client.call(
                "ForkConversation",
                {
                    "sourceCascadeId": src_cid,
                    "forkAtStepIndex": fork_idx,
                    "targetForkWorkspace": 1,  # FORK_TARGET_CURRENT_WORKSPACE
                },
                timeout=30.0,
            )
            fork_cid = str(fres.get("newCascadeId") or "").strip()
            if not fork_cid:
                raise RuntimeError(f"ForkConversation returned no newCascadeId: {fres}")

            brain_copied, brain_warnings = _copy_brain_dir(
                root_dir / "brain" / src_cid, root_dir / "brain" / fork_cid
            )
            warnings.extend(brain_warnings)
            warnings.append(
                "Used ForkConversation fallback: timestamps reflect fork creation time."
            )

            client.call(
                "UpdateConversationAnnotations",
                {
                    "cascadeId": fork_cid,
                    "annotations": rpc_annotations,
                    "mergeAnnotations": False,
                },
                timeout=15.0,
            )
            refreshed = client.call("GetAllCascadeTrajectories", {}, timeout=15.0)
            clone_sum = (refreshed.get("trajectorySummaries") or {}).get(fork_cid) or {}
            ctm = clone_sum.get("trajectoryMetadata") or {}
            return CloneResult(
                ok=True,
                mode="fork_fallback",
                source_cascade_id=src_cid,
                clone_cascade_id=fork_cid,
                source_title=src_title,
                clone_title=clone_title,
                pinned=bool((clone_sum.get("annotations") or {}).get("pinned", final_pinned)),
                project_id=str(ctm.get("projectId") or fres.get("newProjectId") or project_id),
                workspace_uris=list(ctm.get("workspaceUris") or workspace_uris),
                created_time=str(clone_sum.get("createdTime") or ""),
                last_modified_time=str(clone_sum.get("lastModifiedTime") or ""),
                last_user_input_time=str(clone_sum.get("lastUserInputTime") or ""),
                step_count=int(clone_sum.get("stepCount") or 0),
                brain_files_copied=brain_copied,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                warnings=warnings,
            )
        except Exception as tier2_exc:
            if fork_cid:
                _cleanup_partial_clone(root_dir, fork_cid, client=client)
            return CloneResult(
                ok=False,
                mode="error",
                source_cascade_id=src_cid,
                clone_cascade_id="",
                source_title=src_title,
                clone_title=clone_title,
                pinned=final_pinned,
                project_id=project_id,
                workspace_uris=workspace_uris,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                warnings=warnings,
                error=f"All clone strategies failed: {tier2_exc}",
            )

    return CloneResult(
        ok=False,
        mode="error",
        source_cascade_id=src_cid,
        clone_cascade_id="",
        source_title=src_title,
        clone_title=clone_title,
        pinned=final_pinned,
        project_id=project_id,
        error="No disk database or live LanguageServer available.",
    )


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Clone an Antigravity conversation.")
    parser.add_argument(
        "--source",
        default=os.environ.get("ANTIGRAVITY_CONVERSATION_ID", ""),
        help="Source conversation UUID (defaults to ANTIGRAVITY_CONVERSATION_ID).",
    )
    parser.add_argument("--name", "--title", dest="custom_title", default=None, help="Custom clone title.")
    pin_grp = parser.add_mutually_exclusive_group()
    pin_grp.add_argument("--pin", action="store_true", help="Force pinned=true on the clone.")
    pin_grp.add_argument("--unpin", action="store_true", help="Force pinned=false on the clone.")
    parser.add_argument("--archived", action="store_true", help="Preserve archived=true if source was archived.")
    parser.add_argument("--keep-trigger", action="store_true", help="Do not trim trailing /clone trigger step.")
    parser.add_argument("--dry-run", action="store_true", help="Preview clone metadata without writing files.")
    parser.add_argument("--app-data-dir", default=None, help="Override Antigravity app data directory.")
    parser.add_argument("--json", action="store_true", help="Output structured JSON.")
    args = parser.parse_args(argv)

    pin_override: Optional[bool] = True if args.pin else (False if args.unpin else None)
    res = clone_conversation(
        args.source,
        app_data_dir=args.app_data_dir,
        custom_title=args.custom_title,
        pin_override=pin_override,
        keep_archived=args.archived,
        drop_trailing_running=not args.keep_trigger,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
    else:
        if res.ok:
            print(
                f"[{res.mode}] Cloned '{res.source_title}' ({res.source_cascade_id}) "
                f"-> '{res.clone_title}' ({res.clone_cascade_id}) "
                f"| pinned={res.pinned} | steps={res.step_count} | {res.elapsed_ms}ms"
            )
            if res.warnings:
                for w in res.warnings:
                    print(f"  WARN: {w}")
        else:
            print(f"ERROR: {res.error}", file=sys.stderr)
    return 0 if res.ok else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(_main())
