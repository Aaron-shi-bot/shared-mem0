"""Fixture-only checks. Real user transcripts are never mutated by these tests."""
from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import session_sources as s


class SessionSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mem0-source-fixtures-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, client, records, name="session.jsonl"):
        root = self.root / client
        root.mkdir(exist_ok=True)
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
        return root, path

    def parse(self, client, records, **limits):
        root, path = self.fixture(client, records)
        desc = next(s.discover_sessions({client: root}, include_subagents=True))
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        result = s.parse_session(desc, **limits)
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest(), "Source must remain unchanged")
        return result

    def test_codex_roles_compaction_and_mirror_dedup(self):
        def response(role, text, channel=None):
            return {"type": "response_item", "timestamp": "2026-10-01T01:00:00Z", "payload": {
                "type": "message", "role": role, "channel": channel,
                "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}
        result = self.parse("codex", [
            {"type": "session_meta", "payload": {"id": "codex-a", "cwd": "D:/projects/alpha", "source": "vscode"}},
            response("system", "do not retain system"),
            response("developer", "do not retain developer"),
            response("assistant", "do not retain private reasoning", "analysis"),
            response("user", "# AGENTS.md instructions\nInjected environment"),
            response("user", "Keep the confirmed blue button."),
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Keep the confirmed blue button."}},
            {"type": "response_item", "payload": {"type": "function_call_output", "output": "TOOL SECRET"}},
            response("assistant", "The button is blue.", "final"),
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "The button is blue."}},
            {"type": "compacted", "payload": {"message": "Summary: blue was confirmed."}},
        ])
        self.assertEqual([m["role"] for m in result["messages"]], ["user", "assistant", "assistant"])
        self.assertEqual(result["summary"], "Summary: blue was confirmed.")
        self.assertEqual(result["stats"]["duplicate_mirror_events"], 2)
        self.assertEqual(result["stats"]["injected_context_messages_skipped"], 1)
        self.assertFalse(result["incomplete"])
        for message in result["messages"]:
            self.assertEqual(message["session_id"], "codex-a")
            self.assertEqual(message["cwd"], "D:/projects/alpha")
            self.assertEqual(message["source_fingerprint"], result["source_fingerprint"])

    def test_workbuddy_only_message_text(self):
        result = self.parse("workbuddy", [
            {"type": "session-meta", "sessionId": "wb-a", "meta": {"cwd": "D:/projects/beta"}},
            {"type": "message", "role": "user", "sessionId": "wb-a", "timestamp": 1790816400000,
             "content": [{"type": "input_text", "text": "Use the approved design."}, {"type": "input_image", "image": "not retained"}]},
            {"type": "reasoning", "content": "hidden thoughts"},
            {"type": "function_call", "name": "Bash", "arguments": "irrelevant"},
            {"type": "function_call_result", "output": "tool output"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Approved design saved."}]},
        ])
        self.assertEqual(len(result["messages"]), 2)
        self.assertEqual(result["cwd"], "D:/projects/beta")
        self.assertEqual(result["messages"][0]["timestamp"], "2026-10-01T01:00:00+00:00")
        self.assertNotIn("thoughts", str(result["messages"]))

    def test_codex_internal_context_and_aborted_wrappers_are_not_user_intent(self):
        result = self.parse("codex", [
            {"type": "event_msg", "payload": {"type": "user_message", "message": '<codex_internal_context source="goal">Internal reminder.</codex_internal_context>'}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": '<turn_aborted>The previous turn was interrupted.</turn_aborted>实际用户需求。'}},
        ])
        self.assertEqual([m["text"] for m in result["messages"]], ["实际用户需求。"])
        self.assertEqual(result["stats"]["injected_context_blocks_removed"], 2)

    def test_codex_appshot_tree_removed_without_losing_following_request(self):
        result = self.parse("codex", [
            {"type": "event_msg", "payload": {"type": "user_message", "message": '<appshot data-source="ui">' + ('UI tree node\n' * 2000) + '</appshot>\n## My request\n请修复本项目按钮。'}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": '<appshot>Only UI context</appshot>'}},
        ])
        self.assertEqual([m["text"] for m in result["messages"]], ["## My request\n请修复本项目按钮。"])
        self.assertEqual(result["stats"]["injected_context_blocks_removed"], 2)
        self.assertFalse(result["incomplete"])

    def test_workbuddy_runtime_wrapper_retains_only_real_query(self):
        text = '<system-reminder data-role="user-context">IDENTITY PROMPT<connector-status>private</connector-status></system-reminder>\n<user_query>请生成生活工作台。</user_query>'
        result = self.parse("workbuddy", [{"type": "message", "role": "user", "content": text}])
        self.assertEqual([m["text"] for m in result["messages"]], ["请生成生活工作台。"])
        self.assertEqual(result["messages"][0]["role"], "user")

    def test_workbuddy_replay_deduplicates_originals_and_excludes_tools(self):
        replay = ('<cb_summary><previous_user_message><user_query>Build the app.</user_query></previous_user_message>'
                  '<previous_tool_call>Tool: Bash Arguments: do not execute me</previous_tool_call>'
                  '<previous_assistant_message>Reported build completion.</previous_assistant_message>'
                  '<previous_assistant_message>Unique prior result.</previous_assistant_message></cb_summary>')
        result = self.parse("workbuddy", [
            {"type": "message", "role": "user", "content": "Build the app."},
            {"type": "message", "role": "assistant", "content": "Reported build completion."},
            {"type": "message", "role": "user", "content": replay},
            {"type": "message", "role": "user", "content": replay},
        ])
        self.assertEqual([m["text"] for m in result["messages"]], ["Build the app.", "Reported build completion.", "Unique prior result."])
        self.assertEqual(result["messages"][-1]["role"], "assistant")
        self.assertEqual(result["messages"][-1]["kind"], "summary")
        self.assertEqual(result["stats"]["duplicate_embedded_history_messages"], 5)
        self.assertNotIn("Bash", str(result["messages"]))

    def test_workbuddy_opaque_history_summary_is_not_new_user_request(self):
        summary = '<conversation_history_summary>Prior discussion mentioned literal <cb_summary> markup; app completion was reported.</conversation_history_summary>'
        result = self.parse("workbuddy", [{"type": "message", "role": "user", "content": summary}])
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["messages"][0]["role"], "assistant")
        self.assertEqual(result["messages"][0]["kind"], "summary")
        self.assertIn("literal <cb_summary>", result["messages"][0]["text"])

    def test_workbuddy_new_query_after_replay_is_preserved(self):
        text = ('<cb_summary><previous_user_message>Old request.</previous_user_message></cb_summary>'
                '<system-reminder>New runtime context</system-reminder><user_query>New request.</user_query>')
        result = self.parse("workbuddy", [{"type": "message", "role": "user", "content": text}])
        self.assertEqual([m["text"] for m in result["messages"]], ["Old request.", "New request."])
        self.assertEqual([m["kind"] for m in result["messages"]], ["summary", "message"])

    def test_pi_mixed_content_and_summaries(self):
        result = self.parse("pi", [
            {"type": "session", "id": "pi-a", "cwd": "D:/projects/gamma", "parentSession": "user-fork"},
            {"type": "message", "message": {"role": "user", "content": "Continue the motor controller."}},
            {"type": "message", "message": {"role": "assistant", "timestamp": 1790816400000, "content": [
                {"type": "thinking", "thinking": "private"}, {"type": "toolCall", "name": "read", "arguments": {}},
                {"type": "text", "text": "The threshold is confirmed."}]}},
            {"type": "message", "message": {"role": "toolResult", "content": [{"type": "text", "text": "discard tool output"}]}},
            {"type": "compaction", "summary": "Threshold 25 is confirmed."},
            {"type": "branch_summary", "summary": "A user branch was reviewed."},
        ])
        self.assertEqual(len(result["messages"]), 4)
        self.assertEqual(len(result["summaries"]), 2)
        self.assertFalse(result["subagent"], "A normal Pi fork is not a background agent")
        self.assertNotIn("private", str(result["messages"]))

    def test_claude_user_assistant_summary_and_tool_blocks(self):
        result = self.parse("claude", [
            {"type": "user", "sessionId": "claude-a", "cwd": "D:/projects/delta", "timestamp": "2026-10-01T01:00:00Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "Use SQLite."}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hidden"}, {"type": "tool_use", "name": "Read", "input": {}},
                {"type": "text", "text": "SQLite was selected."}]}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "Do not retain this tool output."}]}},
            {"type": "system", "content": "system prompt"},
            {"type": "summary", "summary": "Decision: SQLite.", "leafUuid": "leaf"},
        ])
        self.assertEqual([m["text"] for m in result["messages"]], ["Use SQLite.", "SQLite was selected.", "Decision: SQLite."])
        self.assertFalse(result["incomplete"])

    def test_subagent_exclusion_and_explicit_marking(self):
        root, path = self.fixture("codex", [{"type": "session_meta", "payload": {
            "id": "child", "cwd": "D:/p", "source": {"subagent": {"other": "guardian"}}}}])
        self.assertEqual(list(s.discover_sessions({"codex": root})), [])
        self.assertTrue(next(s.discover_sessions({"codex": root}, include_subagents=True))["subagent"])
        root2, _ = self.fixture("claude", [{"type": "user", "sessionId": "child", "cwd": "D:/p", "isSidechain": True,
                                          "message": {"role": "user", "content": "Child agent task"}}])
        self.assertEqual(list(s.discover_sessions({"claude": root2})), [])
        desc = next(s.discover_sessions({"claude": root2}, include_subagents=True))
        result = s.parse_session(desc)
        self.assertTrue(result["subagent"])
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["stats"]["sidechain_records_skipped"], 1)

    def test_path_subagent_without_metadata(self):
        root, _ = self.fixture("claude", [{"type": "summary", "summary": "child"}], "x/subagents/agent-123.jsonl")
        self.assertEqual(list(s.discover_sessions({"claude": root})), [])

    def test_same_session_copied_to_archive_is_discovered_once(self):
        records = [{"type": "session_meta", "payload": {"id": "same-session", "cwd": "D:/p", "source": "vscode"}}]
        root, original = self.fixture("codex", records, "active/session.jsonl")
        archive = root / "archive"
        archive.mkdir()
        (archive / "moved.jsonl").write_bytes(original.read_bytes())
        result = list(s.discover_sessions({"codex": [root / "active", archive]}))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["path"], str(original))
        self.assertEqual(result[0]["status"], "ok")

    @unittest.skipUnless(importlib.util.find_spec("zstandard"), "zstandard dependency is not installed")
    def test_codex_zstd_real_stream(self):
        import zstandard
        root, path = self.fixture("codex", [
            {"type": "session_meta", "payload": {"id": "zstd-a", "cwd": "D:/p", "source": "vscode"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Read compressed history."}},
        ])
        compressed = path.with_suffix(".jsonl.zst")
        compressed.write_bytes(zstandard.ZstdCompressor().compress(path.read_bytes()))
        path.unlink()  # Only our disposable fixture.
        desc = next(s.discover_sessions({"codex": root}))
        result = s.parse_session(desc)
        self.assertTrue(desc["compressed"])
        self.assertEqual(result["messages"][0]["text"], "Read compressed history.")
        self.assertFalse(result["incomplete"])

    def test_compressed_failure_is_explicit(self):
        root = self.root / "codex"
        root.mkdir()
        (root / "broken.jsonl.zst").write_bytes(b"not zstd")
        desc = next(s.discover_sessions({"codex": root}))
        result = s.parse_session(desc)
        self.assertTrue(result["incomplete"])
        self.assertTrue(result["warnings"])

    def test_bad_lines_and_invalid_utf8_are_reported(self):
        root, path = self.fixture("pi", [{"type": "message", "message": {"role": "user", "content": "Valid"}}])
        with path.open("ab") as stream:
            stream.write(b'{bad json}\n\xff\xfe\n[1,2]\n')
        result = s.parse_session(next(s.discover_sessions({"pi": root})))
        self.assertTrue(result["incomplete"])
        self.assertEqual(result["stats"]["malformed_lines"], 2)
        self.assertEqual(result["stats"]["non_object_lines"], 1)
        self.assertEqual(result["messages"][0]["text"], "Valid")

    def test_oversize_line_is_drained_and_next_line_is_retained(self):
        result = self.parse("pi", [
            {"type": "message", "message": {"role": "toolResult", "content": "X" * 10000}},
            {"type": "message", "message": {"role": "user", "content": "Final decision"}},
        ], max_line_bytes=200)
        self.assertEqual(result["stats"]["oversize_lines"], 1)
        self.assertEqual(result["messages"][0]["text"], "Final decision")
        self.assertTrue(result["truncated"])

    def test_input_byte_limit_is_not_silent(self):
        result = self.parse("pi", [{"type": "message", "message": {"role": "user", "content": "X" * 500}}], max_input_bytes=64)
        self.assertTrue(result["incomplete"])
        self.assertTrue(result["truncated"])
        self.assertIn("input_limit_reached", result["warnings"])

    def test_exact_eof_limit_is_not_truncated(self):
        root, path = self.fixture("pi", [{"type": "message", "message": {"role": "user", "content": "OK"}}])
        desc = next(s.discover_sessions({"pi": root}))
        result = s.parse_session(desc, max_input_bytes=path.stat().st_size)
        self.assertFalse(result["incomplete"])

    def test_tail_budget_and_message_shortening_are_explicit(self):
        result = self.parse("pi", [
            {"type": "message", "message": {"role": "user", "content": "First " * 100}},
            {"type": "message", "message": {"role": "assistant", "content": "Last " * 100}},
        ], max_text_chars=100, max_message_chars=100)
        self.assertEqual(len(result["messages"]), 1)
        self.assertTrue(result["messages"][0]["text"].startswith("Last"))
        self.assertIn("middle omitted", result["messages"][0]["text"])
        self.assertTrue(result["messages"][0]["text_truncated"])
        self.assertEqual(result["stats"]["messages_dropped_for_budget"], 1)
        self.assertTrue(result["incomplete"])

    def test_discovery_change_detected_and_provenance_preserved(self):
        root, path = self.fixture("pi", [{"type": "session", "id": "stable", "cwd": "D:/p"}])
        desc = next(s.discover_sessions({"pi": root}))
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "message", "message": {"role": "user", "content": "new"}}) + "\n")
        result = s.parse_session(desc)
        self.assertIn("source_changed_since_discovery", result["warnings"])
        self.assertNotEqual(result["source_fingerprint"], desc["fingerprint"])
        self.assertEqual(result["messages"][0]["session_id"], "stable")

    def test_repeated_same_source_messages_are_not_deduplicated(self):
        event = {"type": "event_msg", "payload": {"type": "user_message", "message": "Try again."}}
        result = self.parse("codex", [event, event])
        self.assertEqual(len(result["messages"]), 2)

    def test_missing_roots_and_unsupported_client(self):
        self.assertEqual(list(s.discover_sessions({})), [])
        self.assertEqual(list(s.discover_sessions({"pi": self.root / "missing"})), [])
        with self.assertRaises(ValueError):
            list(s.discover_sessions({"unknown": self.root}))

    def test_default_overrides_stay_in_explicit_client_directories(self):
        with patch.dict("os.environ", {"CODEX_HOME": str(self.root / "cx"), "CLAUDE_CONFIG_DIR": str(self.root / "cc"),
                                      "PI_CODING_AGENT_DIR": str(self.root / "pa")}):
            roots = s.default_roots()
        self.assertEqual(roots["codex"], [self.root / "cx/sessions", self.root / "cx/archived_sessions"])
        self.assertEqual(roots["claude"], [self.root / "cc/projects"])
        self.assertEqual(roots["pi"], [self.root / "pa/sessions"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
