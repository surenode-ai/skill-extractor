# skill-extractor

[![ci](https://github.com/surenode-ai/skill-extractor/actions/workflows/ci.yml/badge.svg)](https://github.com/surenode-ai/skill-extractor/actions/workflows/ci.yml)

Mine **reusable skills** from your coding agents' transcripts. Works with
**Claude Code**, **OpenAI Codex CLI**, and **any other agent** via a small
exporter. Every candidate is scored by **confidence** and **utility**, weighted
by how the trace it came from actually turned out (successful / meh / failed),
and only the strong, novel ones are surfaced for your review. You approve,
edit, or reject each one from a **VS Code panel** or the **`/review-skills`**
command. Approved skills are written as real `SKILL.md` files; every candidate,
approved or not, is kept so mining keeps improving.

## Why I built skill-extractor

I kept telling my coding agent the same things: run the tests before saying
it's done, stop hardcoding ports in parallel test suites, reuse fixes we had
already worked out. By the next session, those patterns were gone. Skills
solve this, but writing one by hand after finishing a task is extra work most
people skip.

Agent platforms usually answer with built-in memory: the agent learns from
your sessions, its behavior changes, and you don't see exactly what it
learned. I didn't want that running over transcripts with my code,
infrastructure details, and occasional pasted secrets.

skill-extractor makes that process visible. It runs on your machine, keeps
state as plain JSONL, pattern-redacts secrets before anything reaches a model,
and only installs a persistent instruction after risk lint and your approval.
The result is a markdown skill you can open, edit, or delete.

For one developer, it turns repeated fixes into reusable skills. For a team,
it produces reviewable material for a
[shared skill catalog](#using-this-on-a-team).

## Quick start

```bash
./install.sh     # macOS: launchd timer + VS Code panel + /review-skills
# then in VS Code: Cmd+Shift+P -> "Developer: Reload Window"
```

Not on macOS? The engine is portable; run it under any supervisor:

```bash
python3 engine/extractor.py --loop 1800    # mine every 30 min, forever
```

Review from wherever you work: the VS Code panel pops up when skills are
discovered, `/review-skills` runs the same flow in Claude Code, and
`python3 engine/review.py list` is the raw CLI. Interval, model, scope, and
sources live in `config.json` (`SKILL_EXTRACTOR_INTERVAL=900 ./install.sh`
changes the timer).

## Security model: read this before installing

Be clear-eyed about what this tool is. It is a loop with three powerful parts:

1. **It mines your private agent traces.** Your transcripts contain your code,
   your infrastructure names, your mistakes, and sometimes pasted secrets.
   Reading and segmentation happen entirely on your machine; state files are
   created `0600` in `0700` directories. Keep projects you cannot afford to
   leak out of mining altogether with `scope` / `exclude_projects`.

2. **It may send excerpts to a remote LLM.** The mining prompt (transcript
   excerpts: user messages, tool inputs and outputs) goes to the backend you
   configure. Secret redaction is on by default: private keys, JWTs, AWS/API
   keys, bearer tokens, `SECRET=`/`TOKEN=` env lines, and URL-embedded
   passwords become `[redacted:...]` before any prompt leaves the process.
   Redaction is pattern-based and best-effort, not a guarantee. The default
   backend is your local `claude` CLI, which means Anthropic's API; any other
   backend requires an explicit `ack_command_backend: true`. For **zero
   egress**, point the command backend at a local model:

   ```jsonc
   "mining_backend": "command",
   "mining_command": ["ollama", "run", "llama3"],
   "ack_command_backend": true
   ```

3. **Installed skills change your agent's future behavior.** A mined skill is
   model output; once installed it is a persistent instruction your agent will
   follow in later sessions. Every install therefore runs a risk lint
   (pipe-to-shell bootstraps, credential-file access, disabled safety flags,
   exfiltration shapes, hidden persistence, prompt-injection phrasing, broad
   destructive commands). Flagged skills refuse to install until you
   explicitly acknowledge the findings (`--acknowledge-risk` on the CLI, a
   modal confirmation in VS Code), and the acknowledgement is recorded in
   `decisions.jsonl`. The lint surfaces risk; it does not certify safety.
   Installing a skill is closer to merging code than dismissing a
   notification. Treat it that way.

Private traces in, model in the middle, agent instructions out. The defaults
are privacy-first, but the loop is only as safe as your review of what you
install and your choice of what to mine. Details: [SECURITY.md](SECURITY.md).

## What it supports

- **Multiple agents, one queue.** Pluggable trace sources share one cursor,
  one budget, and one review queue: `claude_code`, `codex` (project identity
  from the session `cwd`, imported sessions skipped to avoid double-mining,
  exec exit codes feed outcome scoring), and `jsonl_dir` for any other agent
  via a ~30-line exporter to the canonical record shape.
- **Six skill categories**, not just technical procedures: techniques,
  workflows, standing preferences, guardrails learned from failures,
  automations, domain knowledge.
- **Outcome-weighted scoring.** Candidates from traces that demonstrably
  worked score higher; failures still teach guardrails.
- **A learning loop.** Your install/reject decisions (with comments) calibrate
  future mining prompts and score priors toward your taste.
- **Incremental and cheap.** Byte cursors and segment fingerprints mean idle
  runs make zero LLM calls; `max_segments_per_run` caps spend.
- **Any scheduler, any mining LLM.** launchd installer on macOS; `--loop`
  anywhere; systemd/cron/Task Scheduler recipes and non-Claude mining backends
  in [docs/PORTABILITY.md](docs/PORTABILITY.md).

## Usage

```bash
python3 engine/extractor.py            # run a mining pass now
python3 engine/extractor.py --status   # counts: scratch / pending / installed / decided
python3 engine/extractor.py --full     # ignore cursor, re-scan all history
python3 engine/extractor.py --self-test    # sanity-check the pipeline, no traces needed

python3 engine/review.py list          # pending candidates (JSON, with risk labels)
python3 engine/review.py show <id>     # full candidate
python3 engine/review.py install <id> [--edits edits.json] [--acknowledge-risk]
python3 engine/review.py reject  <id> --comment "why"
```

Enable more sources in `config.json`:

```jsonc
"sources": ["claude_code", "codex"],   // default: claude_code only
"jsonl_dirs": ["~/my-agent-traces"]    // any agent, via canonical JSONL
```

## Scoring and learning

| Category | What it captures | Outcome weight* |
|---|---|---|
| `technique` | concrete technical procedures (commands, debugging recipes) | 0.45 |
| `workflow` | multi-step ways of organizing work | 0.40 |
| `preference` | standing instructions and repeated corrections you keep expressing | 0.15 |
| `guardrail` | what-to-avoid learned from failures | 0.10 |
| `automation` | repeated manual sequences that could be one skill | 0.35 |
| `domain` | reusable domain knowledge / heuristics | 0.30 |

*How much the source trace's outcome scales utility. A `technique` from a
failed trace is suspect; a `preference` or `guardrail` expressed in a failed
trace is just as valid. Conversation-heavy segments are mined too; that is
where preferences hide.

**Scoring**: `confidence` (is this real and generalizable?) and `utility`
(how valuable if reused?), blended with measured outcome quality: tool error
rate, user sentiment, and the next task's opening message as trailing
feedback. Only candidates clearing the thresholds in `config.json` surface
for review; everything else stays in scratch, never deleted.

**Learning**: every decision feeds back twice. The miner sees your recent
installs and rejections with your stated reasons (propose more like these,
none like those), and per-category approval rates nudge scores toward your
demonstrated taste, capped so a few decisions cannot swing everything. This is
why rejections ask for a comment: "too project-specific" teaches the miner
more than a silent dismissal.

## Architecture

```
 trace sources (adapters)          claude_code | codex | jsonl_dir
            │
            ▼
 extractor.py: incremental read -> segment by task -> heuristic outcome
   -> redact -> mine (your LLM backend) -> score -> dedupe
   -> candidates.jsonl (scratch) -> pending.json (surfaced)
            │
            ├─ VS Code panel / /review-skills / review.py
            ├─ install -> ~/.claude/skills/<name>/SKILL.md + decisions.jsonl
            └─ reject  -> decisions.jsonl (comment kept for learning)
```

| Piece | Path |
|---|---|
| Miner | `engine/extractor.py` |
| Core lib (parsing, scoring, state) | `engine/lib.py` |
| Trace-source adapters | `engine/adapters.py` |
| Review CLI | `engine/review.py` |
| VS Code panel | `vscode-extension/` |
| `/review-skills` flow | `skills/review-skills/` |

State lives in `~/.claude/skill-extractor/state/`: `candidates.jsonl` (every
mined candidate, never deleted), `pending.json` (awaiting review),
`decisions.jsonl` (your calls + comments), `cursor.json` (incremental reads).

Embedding this in your own application? [docs/INTEGRATION.md](docs/INTEGRATION.md)
has the Python API, CLI/JSON contracts, and data schemas, with a runnable
[examples/embed_minimal.py](examples/embed_minimal.py).

## Using this on a team?

skill-extractor installs approved skills locally, for you. If your team needs
mined skills to flow through shared review, risk scanning, policy checks, and
an audit trail before anyone's agent uses them, that governance layer is what
[Surenode](https://surenode.ai) builds on top of this engine: same mining, with
a governed catalog instead of a local folder.

## Requirements

- **Python 3.9+** (the engine is stdlib only, no pip installs)
- A mining LLM: the **Claude Code CLI** by default (no API key needed), or any
  command backend, including fully local models
- macOS for the bundled installer; any OS for the engine itself
- **VS Code** optional (`/review-skills` and the CLI work without it)

## Contributing

```bash
python3 -m pytest tests/
```

See [CONTRIBUTING.md](CONTRIBUTING.md): the engine stays stdlib-only, new
agents arrive as adapters, and commits are signed off (DCO).

## Uninstall

```bash
./uninstall.sh           # removes timer/hook/extension/skill, keeps mined state
./uninstall.sh --purge   # also deletes the state store
```

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
