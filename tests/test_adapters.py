"""Trace-source adapter tests.

Run with:  python3 -m pytest tests/            (or python3 -m unittest discover)
The engine is stdlib-only; pytest is a dev-only convenience.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

import adapters  # noqa: E402


CWD = "/Users/alice/work/My Project"
SESSION_ID = "019f-test-session"

CODEX_TRACE = [
    {"timestamp": "t0", "type": "session_meta",
     "payload": {"id": SESSION_ID, "cwd": CWD}},
    # event_msg mirrors of the same content must NOT be double-counted
    {"timestamp": "t1", "type": "event_msg",
     "payload": {"type": "user_message", "message": "duplicate stream"}},
    {"timestamp": "t1", "type": "response_item",
     "payload": {"type": "message", "role": "user", "content": [
         {"type": "input_text", "text": "Fix the flaky port collisions."}]}},
    {"timestamp": "t2", "type": "response_item",
     "payload": {"type": "function_call", "name": "exec_command",
                 "arguments": "{\"cmd\":\"pytest -q\"}", "call_id": "c1"}},
    {"timestamp": "t3", "type": "response_item",
     "payload": {"type": "function_call_output", "call_id": "c1",
                 "output": "48 passed\nProcess exited with code 0"}},
]


def _write_session(tmp_path, records, name="rollout-1.jsonl"):
    root = tmp_path / "sessions" / "2026" / "07" / "07"
    root.mkdir(parents=True, exist_ok=True)
    p = root / name
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return tmp_path / "sessions", p


def test_codex_identity_comes_from_session_meta(tmp_path):
    root, _ = _write_session(tmp_path, CODEX_TRACE)
    sources = adapters.CodexAdapter().discover({"codex_sessions_dir": str(root)})
    assert len(sources) == 1
    assert sources[0].project == "users-alice-work-my-project"
    assert sources[0].session_id == SESSION_ID


def test_codex_maps_records_without_event_msg_double_counting():
    ad = adapters.CodexAdapter()
    mapped = [r for r in (ad.map_record(rec) for rec in CODEX_TRACE) if r]
    kinds = [(r["type"], r["message"]["content"][0]["type"]
              if isinstance(r["message"]["content"], list) else "text")
             for r in mapped]
    assert kinds == [("user", "text"), ("assistant", "tool_use"), ("user", "tool_result")]
    assert not any("duplicate" in json.dumps(r) for r in mapped)


def test_codex_marks_nonzero_exec_exit_code_as_error():
    ad = adapters.CodexAdapter()
    rec = ad.map_record({"type": "response_item",
                         "payload": {"type": "function_call_output", "call_id": "c",
                                     "output": "boom\nProcess exited with code 2"}})
    assert rec["message"]["content"][0]["is_error"] is True
    ok = ad.map_record({"type": "response_item",
                        "payload": {"type": "function_call_output", "call_id": "c",
                                    "output": "fine\nProcess exited with code 0"}})
    assert ok["message"]["content"][0]["is_error"] is False


def test_imported_sessions_skipped_by_default_and_toggle_includes(tmp_path):
    filler = [{"timestamp": "t", "type": "response_item",
               "payload": {"type": "message", "role": "assistant",
                           "content": [{"type": "output_text", "text": f"preface {i}"}]}}
              for i in range(70)]
    imported = [CODEX_TRACE[0], *filler,
                {"timestamp": "t", "type": "response_item",
                 "payload": {"type": "message", "role": "assistant",
                             "content": [{"type": "output_text",
                                          "text": "[external_agent_tool_call: Read]\ninput: {}"}]}},
                *CODEX_TRACE[2:]]
    root, path = _write_session(tmp_path, imported, name="imported.jsonl")

    ad = adapters.CodexAdapter()
    src = ad.discover({"codex_sessions_dir": str(root)})[0]
    # default: skipped even with the marker ~70 records deep, cursor advances
    records, new_offset = ad.read(src, 0)
    assert records == [] and new_offset > 0

    # a tail appended AFTER the cursor passed the marker is still skipped
    with open(path, "a") as fh:
        fh.write(json.dumps(CODEX_TRACE[2]) + "\n")
    records, _ = ad.read(src, new_offset)
    assert records == []

    # explicit opt-in mines it
    ad2 = adapters.CodexAdapter()
    src2 = ad2.discover({"codex_sessions_dir": str(root),
                         "include_imported_codex_sessions": True})[0]
    records, _ = ad2.read(src2, 0)
    assert records  # mapped canonical records flow through


def test_jsonl_dir_adapter_discovers_canonical_sessions(tmp_path):
    d = tmp_path / "my-agent-traces"
    d.mkdir()
    (d / "sess-42.jsonl").write_text(json.dumps(
        {"type": "user", "message": {"content": "hello"}}) + "\n")
    sources = adapters.JsonlDirAdapter().discover({"jsonl_dirs": [str(d)]})
    assert len(sources) == 1
    assert sources[0].project == "my-agent-traces"
    assert sources[0].session_id == "sess-42"


def test_claude_adapter_respects_scope_and_exclusions(tmp_path, monkeypatch):
    for slug in ("proj-a", "proj-b"):
        p = tmp_path / slug
        p.mkdir()
        (p / "s.jsonl").write_text("{}\n")
    monkeypatch.setattr(adapters.ClaudeCodeAdapter, "ROOT", str(tmp_path))
    ad = adapters.ClaudeCodeAdapter()
    assert len(ad.discover({"scope": "all", "exclude_projects": []})) == 2
    assert len(ad.discover({"scope": ["proj-a"], "exclude_projects": []})) == 1
    assert len(ad.discover({"scope": "all", "exclude_projects": ["proj-b"]})) == 1
