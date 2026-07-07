# Running on Linux / Windows / other coding agents

The scheduled job shipped by `install.sh` is **macOS-specific** (a `launchd`
LaunchAgent). Everything else — the mining engine, review CLI, state files —
is stdlib Python and runs anywhere. This guide covers the three swap points:
**scheduler**, **trace source**, and **mining model backend**.

> Honesty note: the engine is developed and tested on macOS. The recipes below
> are standard OS mechanisms but have not been exercised by us on real Linux/
> Windows machines — treat them as supported-but-verify.

---

## 1. Scheduler per OS

The job to schedule is always the same one-liner:
`python3 <repo>/engine/extractor.py` (incremental; idle runs make zero LLM calls).

### Universal (any OS, zero setup) — loop mode
```bash
python3 engine/extractor.py --loop 1800     # mine every 30 min, forever
```
Run it under anything that keeps a process alive: `nohup … &`, tmux, a Docker
container, Windows NSSM, a supervisor. This is the recommended path when you
don't want to touch OS schedulers.

### macOS — launchd (what install.sh sets up)
Already handled by `./install.sh`. Interval via `SKILL_EXTRACTOR_INTERVAL=900 ./install.sh`.

### Linux — systemd user timer (preferred)
`~/.config/systemd/user/skill-extractor.service`:
```ini
[Unit]
Description=Skill extractor mining pass

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 %h/skill-extractor/engine/extractor.py
```
`~/.config/systemd/user/skill-extractor.timer`:
```ini
[Timer]
OnBootSec=2min
OnUnitActiveSec=30min

[Install]
WantedBy=timers.target
```
```bash
systemctl --user daemon-reload && systemctl --user enable --now skill-extractor.timer
```

### Linux — cron (simpler)
```cron
*/30 * * * * /usr/bin/python3 $HOME/skill-extractor/engine/extractor.py >> $HOME/.claude/skill-extractor/logs/cron.log 2>&1
```

### Windows — Task Scheduler
```bat
schtasks /Create /TN "SkillExtractor" /SC MINUTE /MO 30 ^
  /TR "py -3 %USERPROFILE%\skill-extractor\engine\extractor.py"
```
Windows caveats: `osascript` notifications no-op harmlessly; the SessionStart
hook and skills work in Claude Code for Windows unchanged; paths in
`extension-config.json` must be written manually (install.sh is bash — see
README "Install" and replicate its 3 config steps by hand, or run it in Git
Bash/WSL).

---

## 2. Trace sources — mining other coding agents

Sources are pluggable adapters (`engine/adapters.py`), enabled via config:

```jsonc
// config.json
"sources": ["claude_code", "codex", "jsonl_dir"],
"codex_sessions_dir": null,          // default ~/.codex/sessions
"jsonl_dirs": ["~/my-agent-traces"]  // for the generic adapter
```

| Adapter | Agent | Reads |
|---|---|---|
| `claude_code` | Claude Code (default) | `~/.claude/projects/*/*.jsonl` |
| `codex` | OpenAI Codex CLI | `~/.codex/sessions/**/*.jsonl` — maps `message` / `function_call` / `function_call_output` payloads best-effort |
| `jsonl_dir` | **any agent** | directories of `*.jsonl` already in the canonical record shape (INTEGRATION.md §3) |

For an agent we don't ship an adapter for (Aider, Cursor, opencode, …) you have
two options, in order of preference:
1. **Exporter → `jsonl_dir`**: a small script that converts the agent's history
   into canonical-shape JSONL files in a directory. ~30 lines, no engine changes.
2. **New adapter**: subclass `Adapter` in `adapters.py` (implement `discover()`
   and `map_record()`), register it in `ADAPTERS`. The Codex adapter is the
   template.

Cursor offsets, segment fingerprints, dedup, scoring, and the learning loop all
work identically regardless of source.

## 3. Mining model backend — using Codex or any LLM as the miner

By default mining calls the `claude` CLI. To mine with anything else:

```jsonc
// config.json
"mining_backend": "command",
"mining_command": "codex exec --sandbox read-only -"   // prompt on stdin, text on stdout
```

Any command that reads the prompt from stdin and prints the model's reply to
stdout works (`llm -m gpt-4o`, `ollama run llama3`, a curl script against any
API). The engine's JSON parsing is fence/prose-tolerant and does one
strict-JSON retry, so weaker models are handled. Trade-offs vs the claude_cli
backend: no per-token usage accounting (calls are still counted; `max_usd_per_day`
won't see costs, use `max_segments_per_run` as your cost knob), and no model
fallback chain (your command is in charge).

## 4. UI on other platforms/hosts

- The **VS Code extension** works on VS Code for Linux/Windows unchanged (it
  only reads `pending.json` + shells to `review.py`). Copy
  `vscode-extension/` into the extensions dir and write
  `~/.claude/skill-extractor/extension-config.json` with your `python` and
  `engineDir` paths.
- **No VS Code / different editor?** Use `/review-skills` in Claude Code, the
  raw CLI (`review.py list/show/install/reject`), or build your own surface per
  INTEGRATION.md Pattern A (watch `pending.json`, act via `review.py`).
- Installed skills are plain `SKILL.md` files — consumable by Claude Code on
  any OS, or by your own harness.
