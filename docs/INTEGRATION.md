# Integrating Skill Extractor into your application

This guide is for developers embedding the skill-mining functionality into
their own application — rather than running it as the standalone tool described
in the top-level README. It covers the four integration surfaces, the public
Python API, all data contracts, and how to swap out the pieces (trace source,
mining model, scheduler, UI) you want to replace.

Everything is dependency-free: the engine is stdlib-only Python 3.9+, the CLIs
speak JSON on stdout, and all state is plain JSON/JSONL files.

---

## 1. Architecture at a glance

```
trace source ──▶ reconstruct() ──▶ segment_session() ──▶ worth_mining()
                                                             │
                    outcome_signals() ◀── Segment ──▶ mine_segment()  (LLM)
                          │                                  │
                          └────────── score_candidate() ◀────┘
                                           │
                              candidates.jsonl  (scratch — everything, forever)
                                           │
                              rebuild_pending() (dedupe + cap)
                                           │
                                     pending.json ──▶ your UI
                                           │
                            install / reject (+ comment) ──▶ decisions.jsonl
                                           │                        │
                                 ~/.claude/skills/<name>/    learning loop
                                                        (build_learning_context,
                                                         learned_priors)
```

Three layers, each independently replaceable:

| Layer | Component | Replace it when… |
|---|---|---|
| **Engine** | `engine/lib.py` (library), `engine/extractor.py` (pipeline runner) | you want your own traces, model, or scheduler |
| **Actions** | `engine/review.py` (JSON CLI) | never — it's the stable contract both UIs use |
| **UI** | VS Code extension, `/review-skills` skill, SessionStart hook | you have your own app surface |

---

## 2. Integration patterns (pick one)

### Pattern A — File-based (lowest coupling)

> Building a UI on this pattern? Follow [REVIEW-CLIENTS.md](REVIEW-CLIENTS.md):
> the webview/temp-file/risk-gate hygiene checklist the bundled panel follows.
Run our extractor as-is (scheduler of your choice). Your app only:
1. **Watches** `~/.claude/skill-extractor/state/pending.json` (poll or fs-watch)
   → show your own notification/UI when it changes.
2. **Acts** by shelling out to `review.py` (see §4). Never write the state
   files directly — the CLI keeps scratch/pending/decisions consistent.

This is exactly how the bundled VS Code extension works
(`vscode-extension/extension.js` is a reference client, ~150 lines of logic).

### Pattern B — Library embedding (full control)
Import the engine and drive the pipeline yourself:

```python
import sys; sys.path.insert(0, "<unzipped>/skill-extractor/engine")
import lib

cfg = lib.load_config()

# your traces -> our record format (see §5.1), or reuse ours:
records = [...]                                   # list[dict]
events   = lib.reconstruct(records)               # -> list[Event]
segments = lib.segment_session("proj", "sess-1", events, cfg)

for seg in segments:
    if not lib.worth_mining(seg, cfg):
        continue
    signals = lib.outcome_signals(seg)            # heuristic outcome (§5.4)
    learning = lib.build_learning_context()       # calibration from past decisions
    mined = lib.mine_segment(seg, cfg, learning)  # LLM call(s) -> list[dict]
    priors = lib.learned_priors()
    for raw in mined:
        score = lib.score_candidate(raw, signals, priors)  # confidence/utility
        # persist however you like, or lib.append_candidate({...})

# when your user approves:
# install_skill() enforces the risk gate: a flagged candidate raises
# lib.RiskAcknowledgementRequired unless a human has seen the findings.
risks = lib.risk_findings(candidate_dict)         # [] when clean
path = lib.install_skill(candidate_dict,
                         acknowledge_risk=bool(risks))  # only after showing `risks` to the user!
lib.append_decision({"id": candidate_dict.get("id"), "action": "install",
                     "risk": risks, "risk_acknowledged": bool(risks),
                     "path": path})                # keep the audit trail intact
```

Key API (all in `lib.py`; signatures stable within a major version):

| Function | Purpose |
|---|---|
| `load_config() -> dict` | config.json over defaults |
| `iter_transcripts(cfg) -> list[str]` | discover Claude Code transcripts in scope |
| `read_new_lines(path, offset) -> (records, new_offset)` | incremental JSONL read |
| `reconstruct(records) -> list[Event]` | raw records → ordered Events |
| `segment_session(project, session_id, events, cfg) -> list[Segment]` | split into task-sized segments; attributes next-segment feedback |
| `worth_mining(seg, cfg) -> bool` | budget gate (tool activity OR instruction-dense user text) |
| `outcome_signals(seg) -> dict` | heuristic success/meh/failure signals |
| `mine_segment(seg, cfg, learning="") -> list[dict]` | LLM mining with model fallback + strict-JSON retry |
| `score_candidate(raw, signals, priors=None) -> dict` | confidence/utility/composite (§5.3) |
| `build_learning_context() -> str` | few-shot calibration block from user decisions |
| `learned_priors() -> dict` | per-category approval rates + tag affinity |
| `rebuild_pending() -> list[dict]` | recompute the deduped, capped review queue |
| `risk_findings(cand) -> list[str]` | risk-lint labels for a candidate's instruction fields ([] when clean) |
| `install_skill(cand, acknowledge_risk=False) -> str` | write `~/.claude/skills/<name>/SKILL.md`; raises `RiskAcknowledgementRequired` for flagged candidates without an acknowledgement |
| `append_candidate / read_candidates / append_decision / read_decisions` | state I/O |
| `RUN_USAGE`, `reset_run_usage()`, `usage_totals(prefix)` | token/cost accounting (§6) |

### Pattern C — Subprocess embedding (any language)
Treat the two CLIs as your API; both print JSON to stdout. Suitable for
Node/Go/Rust/JVM hosts. See §4 for the command contract.

### Pattern D — Replace the mining backend
`lib._call_claude(prompt, model, cfg)` is the single choke-point for LLM
access (~30 lines). To use the Anthropic API directly (or another provider),
replace its body: send `prompt`, return the model's text. Everything else —
prompting, parsing, retry, fallback, scoring — is model-agnostic. The prompt
is built by `render_mine_prompt(segment_text, learning)`; expected model
output is a JSON array per §5.2 (parsing tolerates prose around it).

---

## 3. Mining your own (non-Claude-Code) traces

`reconstruct()` accepts a list of records in this minimal shape — adapt your
app's logs to it and the whole pipeline works unchanged:

```jsonc
// user turn
{"type": "user", "message": {"content": "text of the user message"}}

// assistant turn: text and/or tool calls
{"type": "assistant", "message": {"content": [
  {"type": "text", "text": "..."},
  {"type": "tool_use", "name": "ToolName", "input": {"arg": "value"}}
]}}

// tool result (nested in a user-role record, as in the Claude API)
{"type": "user", "message": {"content": [
  {"type": "tool_result", "is_error": false, "content": "output text"}
]}}
```

Only three things matter for quality:
- **genuine user text** (drives segmentation + sentiment + preference mining),
- **tool_use/tool_result pairs with `is_error`** (drives outcome heuristics),
- **chronological order** (drives feedback attribution).

---

## 4. CLI contract (`engine/review.py`)

All commands print JSON to stdout; non-zero exit on not-found errors.

| Command | Returns |
|---|---|
| `review.py list` | pending queue, slim records: `id,name,category,title,description,confidence,utility,composite,trace_outcome,status,source` |
| `review.py list --all` | every scratch candidate (full records) |
| `review.py show <id>` | full candidate record (§5.2) |
| `review.py install <id> [--edits FILE] [--comment STR]` | `{ok,action,path,name}` — writes SKILL.md, logs decision, rebuilds pending |
| `review.py reject <id> [--comment STR]` | `{ok,action,id,comment}` — logs decision; candidate stays in scratch |
| `review.py edit <id> --edits FILE` | `{ok,action,id}` — persist edits without installing |
| `review.py count` | `{pending,scratch,installed,decided}` |
| `review.py export-pending` | full pending records (what the popup renders) |

`--edits FILE` is JSON with any of `name,title,description,trigger,body,tags`.

`engine/extractor.py` flags: *(none)*=incremental run, `--full`, `--dry-run`,
`--self-test`, `--status`. Exit code of a normal run = pending count (≤250).

**Comments matter**: the strings passed via `--comment` are fed verbatim into
future mining prompts as calibration ("user said: …"). Encourage your users to
give reasons — it is the learning signal.

---

## 5. Data contracts

All state lives in `~/.claude/skill-extractor/state/`. Append-only files are
safe to read concurrently; write only through `review.py`/`lib`.

### 5.1 Files

| File | Format | Contents |
|---|---|---|
| `candidates.jsonl` | JSONL, append-only | every candidate ever mined (never deleted) |
| `pending.json` | JSON array | current review queue: surfaced, undecided, deduped, capped at `max_pending` |
| `decisions.jsonl` | JSONL, append-only | user actions + comments (§5.5) |
| `cursor.json` | JSON object | transcript path → byte offset (incremental reads) |
| `mined_segments.json` | JSON array | fingerprints of segments already mined (never re-mined) |
| `usage.jsonl` | JSONL | per-run token/cost ledger (§6) |

### 5.2 Candidate record

```jsonc
{
  "id": "9f2c31ab04d1",            // unique; the handle for install/reject
  "key": "sha1-16",                 // dedup key (normalized name+description)
  "created": "2026-07-05T14:07:03",
  "name": "verify-before-done",     // kebab-case slug; becomes skills/<name>/
  "category": "preference",         // technique|workflow|preference|guardrail|automation|domain
  "title": "Verify before claiming done",
  "description": "One sentence, used as SKILL.md frontmatter description.",
  "trigger": "When to reach for this skill.",
  "body": "Markdown procedure/rules.",
  "tags": ["testing", "verification"],
  "trace_outcome": "success",       // success|meh|failure (miner's judgment)
  "outcome_reason": "evidence cited by the miner",
  "score": {
    "confidence": 0.88,             // is this a real, generalizable skill?
    "utility": 0.79,                // how valuable if reused?
    "outcome_quality": 0.74,        // blended heuristic+LLM trace outcome
    "composite": 0.84,              // 0.5*confidence + 0.5*utility (ranking key)
    "learned_prior": 0.71           // present once user decisions exist
  },
  "signals": { "tool_calls": 5, "tool_errors": 0, "error_rate": 0.0,
               "positive_signals": 2, "negative_signals": 0,
               "heuristic_score": 0.74, "heuristic_label": "success" },
  "source": { "project": "<project-slug>", "session_id": "<uuid>", "segment": 14 },
  "status": "scratch",
  "surfaced": true                  // cleared thresholds -> eligible for pending
}
```

### 5.3 Scoring semantics
- `confidence` = 0.8·LLM self-assessment + evidence bonus (tool activity; only
  for doing-categories) + error-rate tempering.
- `utility` = LLM utility × outcome scaling, where the **outcome weight is
  per-category** (`SKILL_CATEGORIES[cat]["outcome_weight"]`): techniques from
  failed traces are discounted; preferences/guardrails barely are.
- Once decisions exist, both are nudged toward `learned_prior`
  (per-category approval rate + tag affinity), influence = 5%/decision, cap 30%.
- Surfacing requires `min_confidence`, `min_utility`, `surface_threshold`
  (composite) from config.

### 5.4 Outcome signals
Heuristics per segment: tool error rate, positive/negative sentiment regexes
over genuine user text, and **trailing feedback** — the opening user message of
the *next* segment, where "thanks, works" / "still broken" actually lands.

### 5.5 Decision record

```jsonc
{ "id": "9f2c31ab04d1", "key": "…", "name": "verify-before-done",
  "action": "install",              // or "reject"
  "path": "/Users/x/.claude/skills/verify-before-done/SKILL.md",  // install only
  "comment": "renamed; great pattern",
  "edited": true, "score": { … }, "ts": "2026-07-05T15:01:22" }
```

### 5.6 Installed skill format
`install_skill()` writes standard Claude Code skill files:

```
~/.claude/skills/<name>/SKILL.md
---
name: <name>
description: <description>
---
# <title>
**When to use:** <trigger>
<body>
```
Any host that understands SKILL.md (Claude Code, your own agent) can consume
them; there is nothing extractor-specific in the output.

---

## 6. Token/cost accounting & budgets

Every LLM call's usage is harvested from the CLI JSON envelope into
`lib.RUN_USAGE` and flushed per-run to `usage.jsonl`:

```jsonc
{ "ts": "2026-07-05T15:20:11", "calls": 8, "input_tokens": 24,
  "output_tokens": 4120, "cache_read_tokens": 159920, "cache_write_tokens": 19990,
  "cost_usd": 0.31, "model": "sonnet", "surfaced": 5 }
```

Budgets (config): `max_segments_per_run` (LLM calls), `max_tokens_per_run`,
`max_usd_per_day` (checked against the ledger). When any trips, the run stops
cleanly and resumes where it left off next run — fingerprints/cursor guarantee
no re-spend. Measured baseline (sonnet): ~$0.14 first call (cold prompt cache),
~$0.02–0.03 warm; idle runs (no new trace content) make zero calls.

`extractor.py --status` prints spend today / all-time.

---

## 7. Config reference (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `model` | `"sonnet"` | mining model (claude CLI alias or full id) |
| `model_fallbacks` | `["haiku"]` | tried in order on process-level failure |
| `scope` | `"all"` | `"all"` or list of project-slug substrings |
| `exclude_projects` | `[]` | slug substrings to skip |
| `max_segments_per_run` | 8 | LLM calls per run (linear cost knob) |
| `max_tokens_per_run` | 150000 | hard token ceiling per run |
| `max_usd_per_day` | 2.0 | hard daily cost ceiling |
| `max_segment_chars` | 22000 | segment truncation before prompting |
| `min_segment_chars` | 700 | floor for doing-heavy segments |
| `min_tool_calls` | 2 | "doing-heavy" threshold |
| `min_user_chars` / `min_user_messages` | 300 / 2 | conversation-only qualification |
| `min_confidence` / `min_utility` / `surface_threshold` | .55/.40/.55 | surfacing gates |
| `max_pending` | 20 | review-queue cap (rest stays in scratch) |
| `dedup_similarity` | 0.5 | token-Jaccard threshold for near-dupes |
| `mine_timeout_sec` | 240 | per-call subprocess timeout |

---

## 8. Porting notes

- **Scheduler**: launchd is macOS-only. On Linux use cron/systemd-timer around
  `python3 engine/extractor.py`; on Windows, Task Scheduler. The engine itself
  is portable.
- **Desktop notification**: `lib.macos_notify()` uses `osascript`; stub or
  replace it (`notify-send` on Linux). It is best-effort and safe to no-op.
- **Hook**: `hooks/session_start.py` is plain Python emitting the Claude Code
  SessionStart `additionalContext` JSON; reuse or drop.
- **State location**: override by editing the `STATE_ROOT` constant at the top
  of `lib.py` (single definition point).

## 9. Reference clients

- `vscode-extension/extension.js` — Pattern A client: watch pending.json,
  toast, webview, act via review.py.
- `skills/review-skills/SKILL.md` — conversational client: an agent walks the
  user through the same flow via the CLI.
- `examples/embed_minimal.py` — Pattern B: mine a custom in-memory trace end
  to end in ~40 lines.
