#!/usr/bin/env python3
"""
Skill Extractor — periodic miner.

Run by launchd on an interval AND on demand. Incrementally reads new transcript
content across all in-scope projects, segments each session, mines reusable
skills with the local `claude` CLI, scores them (confidence + utility blended
with the trace outcome), dedupes, and records every candidate to the scratch
store. High-scoring, novel candidates are "surfaced" into the pending queue,
which drives the VS Code popup and the /review-skills command.

Usage:
  python3 extractor.py                 # normal incremental run
  python3 extractor.py --full          # ignore cursor, re-scan everything
  python3 extractor.py --dry-run       # mine but don't write state
  python3 extractor.py --self-test     # inject a synthetic trace and mine it (no CLI needed for parsing)
  python3 extractor.py --status        # print counts and exit
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib  # noqa: E402
import adapters  # noqa: E402


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def process_segment(seg: lib.Segment, cfg: dict, existing_keys: set,
                    installed: set, dry_run: bool,
                    learning: str = "", priors: dict | None = None) -> list[dict]:
    """Mine one segment → scored, deduped candidate records. Returns surfaced ones."""
    if not lib.worth_mining(seg, cfg):
        return []

    signals = lib.outcome_signals(seg)
    mined = lib.mine_segment(seg, cfg, learning)
    if not mined:
        return []

    surfaced_now: list[dict] = []
    for raw in mined:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        score = lib.score_candidate(raw, signals, priors)
        cat = str(raw.get("category", "technique")).lower()
        if cat not in lib.SKILL_CATEGORIES:
            cat = "technique"
        cand = {
            "id": uuid.uuid4().hex[:12],
            "created": _now(),
            "name": lib.slugify(raw.get("name")),
            "category": cat,
            "title": raw.get("title", raw.get("name")),
            "description": raw.get("description", ""),
            "trigger": raw.get("trigger", ""),
            "body": raw.get("body", ""),
            "tags": raw.get("tags", []),
            "trace_outcome": raw.get("trace_outcome", signals["heuristic_label"]),
            "outcome_reason": raw.get("outcome_reason", ""),
            "score": score,
            "signals": signals,
            "source": {
                "project": seg.project,
                "session_id": seg.session_id,
                "segment": seg.index,
            },
            "status": "scratch",
            "surfaced": False,
        }
        cand["key"] = lib.candidate_key(cand)

        # Dedup: skip if we've already recorded this key or already installed a skill by this name.
        if cand["key"] in existing_keys or cand["name"] in installed:
            continue
        existing_keys.add(cand["key"])

        # Surface if novel + above thresholds.
        if (score["confidence"] >= cfg["min_confidence"]
                and score["utility"] >= cfg["min_utility"]
                and score["composite"] >= cfg["surface_threshold"]):
            cand["surfaced"] = True
            surfaced_now.append(cand)

        if not dry_run:
            lib.append_candidate(cand)

    return surfaced_now


def _budgets_exceeded(cfg: dict) -> str:
    """Return a reason string if a token/cost ceiling is hit, else ''."""
    max_tok = cfg.get("max_tokens_per_run", 0)
    if max_tok and (lib.RUN_USAGE["input_tokens"] + lib.RUN_USAGE["output_tokens"]) >= max_tok:
        return f"token budget ({max_tok}/run)"
    max_usd = cfg.get("max_usd_per_day", 0)
    if max_usd:
        today = lib.usage_totals(time.strftime("%Y-%m-%d"))
        if today["cost_usd"] + lib.RUN_USAGE["cost_usd"] >= max_usd:
            return f"daily cost budget (${max_usd})"
    return ""


def run(cfg: dict, full: bool = False, dry_run: bool = False) -> int:
    lib.ensure_dirs()
    lib.reset_run_usage()
    lib.log(f"=== extractor run (full={full} dry={dry_run}) ===")
    if cfg.get("mining_backend") == "command" and cfg.get("max_usd_per_day"):
        lib.log("  note: max_usd_per_day has NO effect on the command backend "
                "(no cost envelope); max_segments_per_run is the spend cap")
    cursor = {} if full else lib.load_cursor()
    existing_keys = lib.existing_candidate_keys()
    installed = lib.installed_skill_names()
    mined_fps = set() if full else lib.load_mined_segments()

    # Learning loop: computed once per run from the user's past decisions.
    # Known-skills block keeps the miner from re-proposing what's already surfaced.
    learning = "\n\n".join(b for b in (lib.build_learning_context(),
                                       lib.build_known_skills_block()) if b)
    priors = lib.learned_priors()
    if priors["n_decisions"]:
        lib.log(f"learning from {priors['n_decisions']} past decision(s); "
                f"cat rates: { {k: round(v,2) for k,v in priors['cat_rate'].items()} }")

    sources = adapters.discover_sources(cfg)
    lib.log(f"scanning {len(sources)} source(s) via adapters: "
            f"{sorted(set(a.name for a, _ in sources)) or cfg.get('sources', ['claude_code'])}")

    llm_budget = cfg["max_segments_per_run"]
    total_surfaced: list[dict] = []
    new_cursor = dict(cursor)

    for adapter, src in sources:
        path = src.path
        offset = cursor.get(path, 0)
        records, new_offset = adapter.read(src, offset)
        if not records:
            new_cursor[path] = new_offset
            continue

        slug = src.project
        session_id = src.session_id
        events = lib.reconstruct(records)
        segments = lib.segment_session(slug, session_id, events, cfg)

        budget_hit = False
        for seg in segments:
            if not lib.worth_mining(seg, cfg):
                continue
            fp = lib.segment_fingerprint(seg)
            if fp in mined_fps:
                continue  # already mined in a prior run — skip for FREE (no budget spent)
            reason = "segment budget" if llm_budget <= 0 else _budgets_exceeded(cfg)
            if reason:
                lib.log(f"  stopping: {reason} reached; will resume next run")
                budget_hit = True
                break
            llm_budget -= 1
            surfaced = process_segment(seg, cfg, existing_keys, installed, dry_run,
                                       learning=learning, priors=priors)
            mined_fps.add(fp)
            total_surfaced.extend(surfaced)
            if surfaced:
                lib.log(f"  + {len(surfaced)} surfaced from {slug}/{session_id}#{seg.index}")
        if not budget_hit:
            # only advance cursor if we didn't break out of budget mid-file
            new_cursor[path] = new_offset
            continue
        # budget hit mid-file: keep old offset so we re-read next run
        break

    if not dry_run:
        lib.save_cursor(new_cursor)
        lib.save_mined_segments(mined_fps)
        pending = lib.rebuild_pending()
        u = lib.RUN_USAGE
        if u["calls"]:
            lib.append_usage_ledger({"model": cfg.get("model"), "surfaced": len(total_surfaced)})
            lib.log(f"usage: {u['calls']} calls, {u['input_tokens']}in/{u['output_tokens']}out tokens "
                    f"({u['cache_read_tokens']} cached), ${u['cost_usd']:.4f}")
        lib.log(f"run complete: {len(total_surfaced)} newly surfaced, {len(pending)} pending total")
        if total_surfaced:
            titles = ", ".join(c.get("title", c.get("name", "")) for c in total_surfaced[:3])
            more = f" +{len(total_surfaced) - 3} more" if len(total_surfaced) > 3 else ""
            lib.macos_notify(
                f"🎓 {len(total_surfaced)} skill{'s' if len(total_surfaced) != 1 else ''} discovered",
                f"{titles}{more} — review in VS Code or run /review-skills",
            )
        return len(pending)
    else:
        lib.log(f"dry run complete: {len(total_surfaced)} would be surfaced")
        return len(total_surfaced)


# --------------------------------------------------------------------------- #
# Self-test: inject a synthetic transcript, run the FULL pipeline on it.
# --------------------------------------------------------------------------- #

SYNTHETIC = [
    {"type": "user", "message": {"content": "The prod build keeps failing with an OOM in the webpack step. Fix it."}},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Let me check the node memory limit and the webpack config."},
        {"type": "tool_use", "name": "Bash", "input": {"command": "node --version && cat webpack.config.js | head"}},
    ]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": False, "content": "v18.17.0\nmodule.exports = { ... }"},
    ]}},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "The OOM is the classic default heap cap. I'll raise it via NODE_OPTIONS and disable source maps in prod, then rebuild."},
        {"type": "tool_use", "name": "Bash", "input": {"command": "NODE_OPTIONS=--max-old-space-size=4096 GENERATE_SOURCEMAP=false npm run build"}},
    ]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": False, "content": "Build succeeded in 92s. Bundle written to dist/."},
    ]}},
    {"type": "user", "message": {"content": "Perfect, that worked. Thanks!"}},
]

# Conversation-heavy segment: implicit ways-of-working, almost no tool activity.
SYNTHETIC_PREFS = [
    {"type": "user", "message": {"content": "Before you tell me something is done, always run the tests yourself and show me the output. Last time you said done and it was broken."}},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Understood — I'll verify with the test suite and paste results before claiming completion."},
        {"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}},
    ]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": False, "content": "Tests: 42 passed, 0 failed"},
    ]}},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "All 42 tests pass — output above. The change is verified done."},
    ]}},
    {"type": "user", "message": {"content": "Good. And like I keep saying — when you show me results, give me a summary table first, then details. Not a wall of text."}},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Noted — summary table first, details after. I'll keep that format going forward."},
    ]}},
    {"type": "user", "message": {"content": "Great, this is exactly the format I want. Thanks."}},
]


def self_test(cfg: dict) -> int:
    lib.log("=== SELF TEST ===")
    total = 0
    for label, records in (("technique-trace", SYNTHETIC), ("ways-of-working-trace", SYNTHETIC_PREFS)):
        print(f"\n================ {label} ================")
        events = lib.reconstruct(records)
        seg = lib.segment_session("__selftest__", label, events, cfg)[0]
        print(f"segment: {seg.char_len()} chars, {seg.tool_calls()} tool calls, "
              f"worth_mining={lib.worth_mining(seg, cfg)}")
        signals = lib.outcome_signals(seg)
        print("outcome signals:", signals)
        print("--- calling miner (models: " + ", ".join(lib.model_chain(cfg)) + ") ---")
        mined = lib.mine_segment(seg, cfg, lib.build_learning_context())
        print(f"miner returned {len(mined)} candidate(s)")
        priors = lib.learned_priors()
        for raw in mined:
            score = lib.score_candidate(raw, signals, priors)
            print(f"  • [{raw.get('category','?')}] {raw.get('name')}  "
                  f"conf={score['confidence']} util={score['utility']} "
                  f"composite={score['composite']} outcome={raw.get('trace_outcome')}")
            print(f"    {raw.get('description','')[:120]}")
        total += len(mined)
    return total


def status() -> None:
    cands = lib.read_candidates()
    pending = lib.load_pending()
    decisions = lib.read_decisions()
    installed = lib.installed_skill_names()
    print(f"candidates (scratch total): {len(cands)}")
    print(f"pending review:             {len(pending)}")
    print(f"decisions logged:           {len(decisions)}")
    print(f"installed skills:           {len(installed)}  {sorted(installed)}")
    today = lib.usage_totals(time.strftime("%Y-%m-%d"))
    alltime = lib.usage_totals()
    print(f"spend today:                {today['calls']} calls, "
          f"{today['input_tokens']+today['output_tokens']} tokens, ${today['cost_usd']:.4f}")
    print(f"spend all-time:             {alltime['calls']} calls, "
          f"{alltime['input_tokens']+alltime['output_tokens']} tokens, ${alltime['cost_usd']:.4f} "
          f"({alltime['runs']} mining runs)")


def loop(cfg: dict, interval: int) -> None:
    """Self-scheduling mode — the OS-agnostic alternative to launchd/systemd/
    Task Scheduler: run under any supervisor (nohup, tmux, docker, NSSM…).
    Idle iterations cost zero LLM calls (fingerprints/cursor short-circuit)."""
    lib.log(f"loop mode: mining every {interval}s (Ctrl-C to stop)")
    while True:
        try:
            run(cfg)
        except Exception as e:  # keep the loop alive on transient failures
            lib.log(f"loop iteration failed: {e!r}")
        time.sleep(max(60, interval))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--loop", type=int, metavar="SECONDS",
                    help="run forever, mining every N seconds (portable scheduler)")
    args = ap.parse_args()

    cfg = lib.load_config()
    if args.status:
        status()
        return
    if args.self_test:
        self_test(cfg)
        return
    if args.loop:
        loop(cfg, args.loop)
        return
    n = run(cfg, full=args.full, dry_run=args.dry_run)
    # exit code carries pending count for shell callers (capped at 250)
    sys.exit(min(n, 250) if not args.dry_run else 0)


if __name__ == "__main__":
    main()
