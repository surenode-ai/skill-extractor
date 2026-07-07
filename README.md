# skill-extractor

Mine **reusable skills** from your coding agents' transcripts. Works with
**Claude Code**, **OpenAI Codex CLI**, and **any other agent** via a small
exporter. Every candidate is scored by **confidence** and **utility**, weighted
by how the trace it came from actually turned out (successful / meh / failed),
and only the strong, novel ones are surfaced for your review. You approve,
edit, or reject each one from a **VS Code panel** or the **`/review-skills`**
command. Approved skills are written as real `SKILL.md` files; every candidate,
approved or not, is kept so mining keeps improving.

## How it works

```
 trace sources (pluggable adapters)
   claude_code   ~/.claude/projects/**/*.jsonl
   codex         ~/.codex/sessions/**/*.jsonl
   jsonl_dir     any agent, via canonical JSONL
            │
            ▼
 ┌─────────────────────────────┐   scheduled (launchd / systemd / cron / --loop)
 │ extractor.py (the miner)    │◀──────────────────────────────────────────
 │  1. incremental read (cursor; idle runs make zero LLM calls)             │
 │  2. reconstruct + segment by task                                        │
 │  3. heuristic outcome (tool errors, user sentiment, next-turn feedback)  │
 │  4. mine with your LLM backend (claude CLI, or any command)              │
 │  5. score: confidence + utility  (blended with trace outcome)            │
 │  6. dedupe → candidates.jsonl (scratch) → pending.json (surfaced)        │
 └─────────────────────────────┘
            │
            ├──▶ VS Code extension  → popup → webview review panel
            └──▶ /review-skills     → terminal review flow
                        │
                        ├─ install → ~/.claude/skills/<name>/SKILL.md  + decisions.jsonl
                        └─ reject  → decisions.jsonl (with your comment); candidate kept in scratch
```

> **Integrating this into your own application?** See
> [docs/INTEGRATION.md](docs/INTEGRATION.md): architecture, Python API,
> CLI/JSON contracts, data schemas, and the runnable
> [examples/embed_minimal.py](examples/embed_minimal.py).
>
> **Linux / Windows / other agents / other mining LLMs?** See
> [docs/PORTABILITY.md](docs/PORTABILITY.md): per-OS schedulers, trace-source
> adapters, and swapping the mining backend.

## Mining more than one agent

Sources are pluggable adapters (`engine/adapters.py`), enabled in `config.json`:

```jsonc
"sources": ["claude_code", "codex"],   // default: claude_code only
"codex_sessions_dir": null,            // default ~/.codex/sessions
"jsonl_dirs": ["~/my-agent-traces"]    // canonical-JSONL escape hatch for ANY agent
```

| Adapter | Agent | Notes |
|---|---|---|
| `claude_code` | Claude Code | native format, no mapping needed |
| `codex` | OpenAI Codex CLI | project identity from the session `cwd`; sessions imported from other agents are skipped by default (no double-mining; opt in with `include_imported_codex_sessions`); exec exit codes feed outcome scoring |
| `jsonl_dir` | any agent | write a ~30-line exporter to the canonical record shape (INTEGRATION.md §3) and point `jsonl_dirs` at it |

All sources share one cursor, one segment budget, and one review queue.

## Requirements

- **Python 3.9+** (the engine is stdlib only, no pip installs)
- A mining LLM: the **Claude Code CLI** by default (no API key needed), or any
  command via `mining_backend: "command"` (see PORTABILITY.md)
- macOS for the bundled `install.sh` (launchd + notifications); every other OS
  runs the same engine via `--loop`, systemd, cron, or Task Scheduler
- **VS Code** for the review panel (optional; `/review-skills` works without it)

## Install

```bash
./install.sh
# then in VS Code: Cmd+Shift+P → "Developer: Reload Window"
```

Optional: change the periodic interval (seconds): `SKILL_EXTRACTOR_INTERVAL=900 ./install.sh`.

Not on macOS? Run the portable scheduler under any supervisor:

```bash
python3 engine/extractor.py --loop 1800    # mine every 30 min, forever
```

## Components

| Piece | Path | Role |
|---|---|---|
| Miner | `engine/extractor.py` | periodic extraction pipeline |
| Core lib | `engine/lib.py` | parsing, segmentation, outcome heuristics, scoring, state I/O |
| Sources | `engine/adapters.py` | pluggable trace-source adapters (claude_code / codex / jsonl_dir) |
| Review CLI | `engine/review.py` | list / show / install / reject, used by both UIs |
| VS Code ext | `vscode-extension/` | popup notification + webview review panel |
| Skill | `skills/review-skills/` | `/review-skills` terminal review flow |
| Hook | `hooks/session_start.py` | session-start banner when skills are pending |
| Config | `config.json` | thresholds, model, scope, sources |

## State (kept in `~/.claude/skill-extractor/state/`)

- `candidates.jsonl` — **every** mined candidate (the scratch list). Never deleted.
- `pending.json` — surfaced candidates awaiting review (drives the popup).
- `decisions.jsonl` — install/reject actions + your comments (the learning signal).
- `cursor.json` — per-transcript byte offsets for incremental reads.

## Usage

```bash
python3 engine/extractor.py            # run a mining pass now
python3 engine/extractor.py --status   # counts: scratch / pending / installed / decided
python3 engine/extractor.py --self-test    # sanity-check the pipeline on a synthetic trace
python3 engine/extractor.py --full     # ignore cursor, re-scan all history
python3 engine/extractor.py --loop 1800    # portable scheduler mode

python3 engine/review.py list          # pending candidates (JSON)
python3 engine/review.py show <id>     # full candidate
python3 engine/review.py install <id> [--edits edits.json] [--comment "..."]
python3 engine/review.py reject  <id> --comment "why"
```

## Skill categories

The miner extracts six kinds of skills, not just technical procedures:

| Category | What it captures | Outcome weight* |
|---|---|---|
| `technique` | concrete technical procedures (commands, debugging recipes) | 0.45 |
| `workflow` | multi-step ways of organizing work (ordering, parallelization) | 0.40 |
| `preference` | **implicit ways-of-working the user keeps expressing**: standing instructions, repeated corrections, formats they ask for. Users often don't realize these are abstractable | 0.15 |
| `guardrail` | what-to-avoid learned from failures | 0.10 |
| `automation` | repeated manual sequences that could be one skill | 0.35 |
| `domain` | reusable domain knowledge / heuristics | 0.30 |

*How much the source trace's outcome scales utility. A `technique` from a failed
trace is suspect; a `preference` or `guardrail` expressed in a failed trace is
just as valid (often more so). Conversation-only segments (few tool calls but
real user engagement) are mined too; that's where preferences hide.

## The learning loop

Every install/reject decision (with your comment) feeds back into mining two ways:

1. **Prompt calibration**: the miner sees your recent installs and rejections
   *with your stated reasons* and is told to propose more like the former,
   none like the latter.
2. **Score priors**: per-category approval rates (Laplace-smoothed) and tag
   affinity nudge confidence/utility toward your demonstrated taste. Influence
   grows with evidence (5%/decision, capped at 30%) so a couple of decisions
   can't swing scores wildly.

This is why rejections ask for a comment: "too project-specific" teaches the
miner more than a silent dismissal.

## Model-agnostic mining

Mining works best-effort with whatever model is available: the configured
`model` is tried first, then each of `model_fallbacks` (default `haiku`) on
process-level failure (unavailable/overloaded/timeout). If a model returns
prose instead of JSON (common with smaller models), one strict-JSON retry is
issued before falling through. Set `"model": "opus"` in `config.json` for the
highest-quality extraction if cost is acceptable. To mine with a non-Claude
LLM entirely, set `mining_backend: "command"` (see PORTABILITY.md §3).

## Scoring

- **confidence**: is this a real, generalizable skill? (miner self-assessment,
  tempered by evidence: amount of tool activity, low error rate).
- **utility**: how valuable if reused? (miner estimate, scaled by the measured
  **outcome quality** of the demonstrating trace).
- **outcome quality**: blend of a heuristic read (tool error rate, positive /
  negative user sentiment, and the *next task's opening message* as trailing
  feedback) and the miner's own honest judgment of the trace. A skill from a
  failed trace still gets recorded, but is down-weighted.

Only candidates clearing `min_confidence`, `min_utility`, and `surface_threshold`
(see `config.json`) are surfaced for review; the rest stay in scratch.

## Using this on a team?

skill-extractor installs approved skills locally, for you. If your team needs
mined skills to flow through shared review, risk scanning, policy checks, and
an audit trail before anyone's agent uses them, that governance layer is what
[Surenode](https://surenode.ai) builds on top of this engine: same mining, with
a governed catalog instead of a local folder.

## Tests

```bash
python3 -m pytest tests/     # or: python3 -m unittest discover tests
```

## Uninstall

```bash
./uninstall.sh           # removes timer/hook/extension/skill, keeps mined state
./uninstall.sh --purge   # also deletes the state store
```

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
