"""Comprehensive test suite for the `antigravity-clone` plugin."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import clone_engine
import clone_pre
import wire_pb


def _encode_sample_metadata(root_cid: str, project_id: str, created_sec: int) -> bytes:
    ts = wire_pb.encode_fields([(1, 0, created_sec), (2, 0, 123456000)])
    return wire_pb.encode_fields(
        [
            (2, 2, ts),
            (6, 2, root_cid.encode("utf-8")),
            (7, 2, b"file:///d%3A/sample-workspace"),
            (18, 2, project_id.encode("utf-8")),
            (999, 0, 42),  # unknown future field must survive round-trip
        ]
    )


def _encode_sample_step(step_type: int, status: int, text: str = "") -> bytes:
    fields = [(1, 0, step_type), (4, 0, status)]
    if text:
        fields.append((19, 2, text.encode("utf-8")))
    return wire_pb.encode_fields(fields)


def _seed_synthetic_db(
    db_path: Path,
    cid: str,
    traj_id: str,
    project_id: str,
    created_sec: int,
    steps: list[tuple[int, int, int, str]],
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE trajectory_meta (trajectory_id TEXT PRIMARY KEY, cascade_id TEXT, trajectory_type INTEGER, source INTEGER)"
    )
    conn.execute(
        "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER NOT NULL DEFAULT 0, status INTEGER NOT NULL DEFAULT 0, has_subtrajectory NUMERIC NOT NULL DEFAULT 0, metadata BLOB, error_details BLOB, permissions BLOB, task_details BLOB, render_info BLOB, step_payload BLOB, step_format INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER NOT NULL DEFAULT 0)")
    conn.execute("CREATE TABLE executor_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
    conn.execute("CREATE TABLE parent_references (idx INTEGER PRIMARY KEY, data BLOB)")
    conn.execute("CREATE TABLE trajectory_metadata_blob (id TEXT PRIMARY KEY, data BLOB)")
    conn.execute("INSERT INTO trajectory_meta VALUES (?, ?, 4, 1)", (traj_id, cid))
    conn.execute(
        "INSERT INTO trajectory_metadata_blob VALUES ('main', ?)",
        (_encode_sample_metadata(cid, project_id, created_sec),),
    )
    conn.execute("INSERT INTO parent_references VALUES (0, ?)", (b"subagent-ref",))
    for idx, stype, status, text in steps:
        conn.execute(
            "INSERT INTO steps (idx, step_type, status, step_payload, step_format) VALUES (?, ?, ?, ?, 0)",
            (idx, stype, status, _encode_sample_step(stype, status, text)),
        )
    conn.commit()
    conn.close()


class TestWirePb(unittest.TestCase):
    def test_round_trip_and_unknown_fields(self) -> None:
        raw = _encode_sample_metadata("old-cid-1", "proj-abc", 1700000000)
        new_cid = "new-completely-different-length-cid-1234567890"
        rewritten = wire_pb.rewrite_string_field(raw, 6, new_cid, only_if_matches="old-cid-1")
        self.assertEqual(wire_pb.extract_first_string(rewritten, 6), new_cid)
        self.assertEqual(wire_pb.extract_first_string(rewritten, 18), "proj-abc")
        self.assertEqual(wire_pb.get_first_varint(rewritten, 999), 42)

    def test_varint_rewrite(self) -> None:
        step = _encode_sample_step(132, 2, "running")
        fixed = wire_pb.rewrite_varint_field(step, 4, 6)
        self.assertEqual(wire_pb.get_first_varint(fixed, 1), 132)
        self.assertEqual(wire_pb.get_first_varint(fixed, 4), 6)

    def test_truncated_raises(self) -> None:
        with self.assertRaises(ValueError):
            wire_pb.parse_fields(b"\x12\x0aabc")


class TestTitleNumbering(unittest.TestCase):
    def test_numbering_sequences(self) -> None:
        self.assertEqual(clone_engine.compute_clone_title("My Chat", {"My Chat"}), "My Chat 1")
        self.assertEqual(
            clone_engine.compute_clone_title("My Chat", {"My Chat", "My Chat 1"}), "My Chat 2"
        )
        # Cloning an existing clone `"My Chat 1"` while `"My Chat"` exists -> `"My Chat 2"`
        self.assertEqual(
            clone_engine.compute_clone_title("My Chat 1", {"My Chat", "My Chat 1"}), "My Chat 2"
        )
        # Numbers that are part of original title (`"Project 2026"`) should NOT get bumped to 2027
        self.assertEqual(
            clone_engine.compute_clone_title("Project 2026", {"Project 2026"}), "Project 2026 1"
        )
        self.assertEqual(
            clone_engine.compute_clone_title("My Chat", {"My Chat"}, custom_title="Forked Branch"),
            "Forked Branch",
        )


class TestPreInvocationParsing(unittest.TestCase):
    def test_explicit_commands_and_xml_wrapping(self) -> None:
        p1 = clone_pre.parse_clone_command("/clone")
        self.assertIsNotNone(p1)
        self.assertIsNone(p1["pin_override"])

        p2 = clone_pre.parse_clone_command(
            "<USER_REQUEST>\n.clone --pin Custom Copy\n</USER_REQUEST>\n<ADDITIONAL_METADATA>time</ADDITIONAL_METADATA>"
        )
        self.assertIsNotNone(p2)
        self.assertTrue(p2["pin_override"])
        self.assertEqual(p2["custom_title"], "Custom Copy")

        p3 = clone_pre.parse_clone_command("/clone --unpin --id 12345678-1234-1234-1234-1234567890ab")
        self.assertIsNotNone(p3)
        self.assertFalse(p3["pin_override"])
        self.assertEqual(p3["source_id"], "12345678-1234-1234-1234-1234567890ab")

    def test_does_not_match_prose(self) -> None:
        self.assertIsNone(clone_pre.parse_clone_command("Create a plugin like /clone for Antigravity."))
        self.assertIsNone(clone_pre.parse_clone_command("How does /clone work?"))
        self.assertIsNone(clone_pre.parse_clone_command("/clone something\nand also do a full refactor below"))

    def test_invocation_num_guard(self) -> None:
        res = clone_pre.handle_pre_invocation({"lastUserInput": "/clone", "invocationNum": 2})
        self.assertEqual(res, {"injectSteps": [], "terminationBehavior": ""})

    def test_help_command(self) -> None:
        res = clone_pre.handle_pre_invocation({"lastUserInput": "/clone --help", "invocationNum": 1})
        self.assertEqual(res["terminationBehavior"], "terminate")
        sys_msg = res["injectSteps"][0]["systemMessage"]
        self.assertNotIn("metadata", sys_msg)
        md = sys_msg["renderInfo"]["markdown"]
        self.assertIn("Antigravity Conversation Cloner", md)

    def test_idempotency_single_clone_per_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_guard = clone_pre.GUARD_FILE
            clone_pre.GUARD_FILE = Path(tmp) / ".clone_guard.json"
            calls = []
            orig_clone = clone_pre.clone_conversation
            try:
                def _fake_clone(target_cid, **kwargs):
                    calls.append((target_cid, kwargs))
                    return clone_engine.CloneResult(
                        ok=True,
                        mode="load_trajectory",
                        source_cascade_id=target_cid,
                        clone_cascade_id="11111111-1111-1111-1111-111111111111",
                        source_title="Demo",
                        clone_title="Demo 1",
                        pinned=False,
                        project_id="",
                    )

                clone_pre.clone_conversation = _fake_clone
                payload_1 = {
                    "conversationId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "executionId": "exec-unique-1",
                    "lastUserInput": "<USER_REQUEST>\n/clone \n</USER_REQUEST>\n<ADDITIONAL_METADATA><SKILL>clone</SKILL></ADDITIONAL_METADATA>",
                    "invocationNum": 0,
                    "initialNumSteps": 10,
                }
                payload_2 = {
                    **payload_1,
                    "invocationNum": 1,
                }
                r1 = clone_pre.handle_pre_invocation(payload_1)
                r2 = clone_pre.handle_pre_invocation(payload_2)
                self.assertEqual(len(calls), 1)
                self.assertEqual(r1["terminationBehavior"], "terminate")
                self.assertNotIn("metadata", r1["injectSteps"][0]["systemMessage"])
                self.assertEqual(r2["terminationBehavior"], "terminate")
            finally:
                clone_pre.clone_conversation = orig_clone
                clone_pre.GUARD_FILE = old_guard


class TestOfflineSandboxClone(unittest.TestCase):
    def test_offline_backup_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp)
            (app_dir / "conversations").mkdir()
            (app_dir / "annotations").mkdir()
            (app_dir / "chapter_titles").mkdir()
            src_cid = str(uuid.uuid4())
            proj_id = str(uuid.uuid4())
            traj_id = str(uuid.uuid4())
            (app_dir / "brain" / src_cid).mkdir(parents=True)
            (app_dir / "brain" / src_cid / "notes.md").write_text("secret plan", encoding="utf-8")
            (app_dir / "chapter_titles" / f"{src_cid}.pb").write_bytes(b"\x0a\x03abc")
            (app_dir / "annotations" / f"{src_cid}.pbtxt").write_text(
                'title:"Offline Chat" pinned:true\n', encoding="utf-8"
            )
            _seed_synthetic_db(
                app_dir / "conversations" / f"{src_cid}.db",
                src_cid,
                traj_id,
                proj_id,
                1700000000,
                [
                    (0, 14, 3, "hello"),
                    (1, 15, 2, "mid-crash-running"),
                    (2, 15, 3, "completed reply"),
                    (3, 14, 3, "/clone"),
                    (4, 132, 2, "running-tool"),
                ],
            )
            # Also seed minimal conversation_summaries.db
            sdb = sqlite3.connect(str(app_dir / "conversation_summaries.db"))
            sdb.execute(
                "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, step_count INTEGER, status TEXT, not_fully_idle INTEGER, killed INTEGER, raw_summary BLOB)"
            )
            raw_sum = wire_pb.encode_fields(
                [
                    (1, 2, b"Offline Chat"),
                    (4, 2, traj_id.encode("utf-8")),
                    (5, 0, 2),
                    (15, 2, wire_pb.encode_fields([(1, 2, b"Offline Chat"), (12, 0, 1)])),
                    (17, 2, wire_pb.encode_fields([(6, 2, src_cid.encode("utf-8"))])),
                ]
            )
            sdb.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?, 5, 'CASCADE_RUN_STATUS_RUNNING', 1, 0, ?)",
                (src_cid, "Offline Chat", raw_sum),
            )
            sdb.commit()
            sdb.close()

            res = clone_engine.clone_conversation(
                src_cid,
                app_data_dir=str(app_dir),
                strip_trigger_input="/clone",
                initial_num_steps=4,
            )
            self.assertTrue(res.ok, res.error)
            self.assertEqual(res.mode, "offline")
            self.assertEqual(res.clone_title, "Offline Chat 1")
            self.assertTrue(res.pinned)
            # Steps 3 (/clone) and 4 (trailing running tool) stripped; step 1 normalized to CANCELED (6)
            self.assertEqual(res.step_count, 3)

            clone_db = app_dir / "conversations" / f"{res.clone_cascade_id}.db"
            conn = sqlite3.connect(str(clone_db))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM parent_references").fetchone()[0], 0)
            st1, payload1 = conn.execute("SELECT status, step_payload FROM steps WHERE idx=1").fetchone()
            self.assertEqual(st1, 6)
            self.assertEqual(wire_pb.get_first_varint(bytes(payload1), 4), 6)
            meta_cid, meta_tid = conn.execute(
                "SELECT cascade_id, trajectory_id FROM trajectory_meta"
            ).fetchone()
            self.assertEqual(meta_cid, res.clone_cascade_id)
            self.assertNotEqual(meta_tid, traj_id)
            blob = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()[0]
            self.assertEqual(wire_pb.extract_first_string(bytes(blob), 6), res.clone_cascade_id)
            conn.close()

            self.assertTrue((app_dir / "brain" / res.clone_cascade_id / "notes.md").is_file())
            self.assertTrue((app_dir / "chapter_titles" / f"{res.clone_cascade_id}.pb").is_file())


class TestLiveLSIntegration(unittest.TestCase):
    def test_live_ls_clone_preserves_timestamps_pinned_workspace_and_live_reload(self) -> None:
        app_dir = clone_engine.get_app_data_dir()
        client, summaries = clone_engine.discover_live_ls(app_dir)
        if client is None or not summaries:
            self.skipTest("No live LanguageServer running for integration test")

        # Borrow binary step template from an active DB (first 2 steps) without mutating source
        sample_cid = next(
            (k for k in summaries if (app_dir / "conversations" / f"{k}.db").is_file()),
            None,
        )
        if not sample_cid:
            self.skipTest("No .db conversation found for seeding ephemeral test")

        src_cid = ""
        clone_cid = ""
        try:
            started = client.call("StartCascade", {"source": 1}, timeout=10.0)
            src_cid = started["cascadeId"]
            client.call("DeleteCascadeTrajectory", {"cascadeId": src_cid}, timeout=10.0)

            src_db = app_dir / "conversations" / f"{sample_cid}.db"
            eph_db = app_dir / "conversations" / f"{src_cid}.db"
            sconn = clone_engine._sqlite_connect_ro(src_db)
            dconn = sqlite3.connect(str(eph_db), timeout=10.0)
            sconn.backup(dconn)
            sconn.close()
            dconn.execute(
                "UPDATE trajectory_meta SET cascade_id=?, trajectory_id=?",
                (src_cid, str(uuid.uuid4())),
            )
            dconn.execute("DELETE FROM steps WHERE idx >= 3")
            dconn.execute("DELETE FROM parent_references")
            blob = dconn.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()[0]
            new_blob = wire_pb.rewrite_string_field(bytes(blob), 6, src_cid)
            dconn.execute("UPDATE trajectory_metadata_blob SET data=? WHERE id='main'", (new_blob,))
            dconn.commit()
            dconn.close()

            (app_dir / "brain" / src_cid).mkdir(parents=True, exist_ok=True)
            (app_dir / "brain" / src_cid / "eph_artifact.txt").write_text("ephemeral proof", encoding="utf-8")

            client.call("LoadTrajectory", {"cascadeId": src_cid}, timeout=15.0)
            client.call(
                "UpdateConversationAnnotations",
                {
                    "cascadeId": src_cid,
                    "annotations": {"title": "__CLONE_E2E_FIXTURE__", "pinned": True},
                    "mergeAnnotations": False,
                },
                timeout=10.0,
            )
            before_sum = client.call("GetAllCascadeTrajectories", {})["trajectorySummaries"][src_cid]

            res = clone_engine.clone_conversation(src_cid, app_data_dir=str(app_dir))
            self.assertTrue(res.ok, res.error)
            self.assertEqual(res.mode, "load_trajectory")
            clone_cid = res.clone_cascade_id
            self.assertEqual(res.clone_title, "__CLONE_E2E_FIXTURE__ 1")
            self.assertTrue(res.pinned)

            after_sum = client.call("GetAllCascadeTrajectories", {})["trajectorySummaries"][clone_cid]
            self.assertEqual(after_sum.get("createdTime"), before_sum.get("createdTime"))
            self.assertEqual(after_sum.get("lastModifiedTime"), before_sum.get("lastModifiedTime"))
            self.assertEqual(after_sum.get("lastUserInputTime"), before_sum.get("lastUserInputTime"))
            self.assertEqual(
                (after_sum.get("trajectoryMetadata") or {}).get("projectId"),
                (before_sum.get("trajectoryMetadata") or {}).get("projectId"),
            )
            self.assertEqual(
                (after_sum.get("trajectoryMetadata") or {}).get("workspaceUris"),
                (before_sum.get("trajectoryMetadata") or {}).get("workspaceUris"),
            )
            self.assertEqual(
                (after_sum.get("trajectoryMetadata") or {}).get("rootConversationId"),
                clone_cid,
            )
            self.assertTrue((after_sum.get("annotations") or {}).get("pinned"))
            self.assertEqual(after_sum.get("status"), "CASCADE_RUN_STATUS_IDLE")
            self.assertTrue((app_dir / "brain" / clone_cid / "eph_artifact.txt").is_file())
        finally:
            if clone_cid:
                clone_engine._cleanup_partial_clone(app_dir, clone_cid, client=client)
            if src_cid:
                clone_engine._cleanup_partial_clone(app_dir, src_cid, client=client)


if __name__ == "__main__":
    unittest.main(verbosity=2)
