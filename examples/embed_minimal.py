#!/usr/bin/env python3
"""
Minimal embedding example (Pattern B in docs/INTEGRATION.md).

Feeds a custom in-memory trace through the full pipeline:
reconstruct -> segment -> outcome heuristics -> LLM mining -> scoring.
Requires a logged-in `claude` CLI. Run:  python3 examples/embed_minimal.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "engine"))
import lib  # noqa: E402

# 1. Your application's trace, adapted to the record shape (INTEGRATION.md §3).
MY_TRACE = [
    {"type": "user", "message": {"content":
        "Our API tests are flaky in CI. Figure out why and fix it."}},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Flaky API tests usually share ports or leak state. Checking for a hardcoded port."},
        {"type": "tool_use", "name": "Bash", "input": {"command": "grep -rn 'PORT = 8080' tests/"}},
    ]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": False, "content": "tests/conftest.py:12: PORT = 8080"},
    ]}},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Hardcoded port — parallel CI workers collide. Switching to an ephemeral port (bind to 0) per worker and re-running."},
        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest tests/ -n 4"}},
    ]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": False, "content": "48 passed in 21.3s (4 workers)"},
    ]}},
    {"type": "user", "message": {"content": "That fixed it, thanks — no more flakes."}},
]

cfg = lib.load_config()

# 2. Reconstruct + segment.
events = lib.reconstruct(MY_TRACE)
segments = lib.segment_session("my-app", "demo-session", events, cfg)
seg = segments[0]
print(f"segments: {len(segments)}; mining segment 0 "
      f"({seg.char_len()} chars, {seg.tool_calls()} tool calls)")

# 3. Heuristic outcome (note: 'thanks' feedback attributed from next segment).
signals = lib.outcome_signals(seg)
print("outcome:", signals["heuristic_label"], signals)

# 4. Mine (LLM), with calibration from any past user decisions.
mined = lib.mine_segment(seg, cfg, lib.build_learning_context())
print(f"mined {len(mined)} candidate(s)")

# 5. Score and print.
priors = lib.learned_priors()
for raw in mined:
    score = lib.score_candidate(raw, signals, priors)
    print(f"\n[{raw.get('category','technique')}] {raw.get('name')}")
    print(f"  {raw.get('description','')}")
    print(f"  scores: {json.dumps(score)}")
    # To persist into the shared store instead:  lib.append_candidate({...})
    # To install directly as a SKILL.md:         lib.install_skill(raw)
