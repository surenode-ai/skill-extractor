---
name: review-skills
description: Review skills that the skill-extractor mined from your Claude Code traces — inspect each candidate's definition and confidence/utility scores, edit it, then install it as a real skill or reject it with a comment. Use when the user runs /review-skills, asks to "review discovered/mined skills", or a session-start banner reports pending skills.
---

# Review discovered skills

The skill-extractor mines reusable procedures ("skills") from Claude Code
conversation + coding traces, scores each by **confidence** (is this a real,
generalizable skill?) and **utility** (how valuable if reused?), and queues the
strong, novel ones for human review. This flow lets the user approve, edit, or
reject them. Every candidate — approved or not — stays on record so mining
improves over time.

The engine lives at `~/.claude/skill-extractor/`. Use the review CLI for all
actions (never hand-edit the state files). Resolve the paths first:

```
PY=$(cat ~/.claude/skill-extractor/extension-config.json | python3 -c "import json,sys;print(json.load(sys.stdin)['python'])")
ENGINE=$(cat ~/.claude/skill-extractor/extension-config.json | python3 -c "import json,sys;print(json.load(sys.stdin)['engineDir'])")
```
(Fallback: `PY=python3`, `ENGINE=~/Nesh/skill-extractor/engine`.)

## Steps

1. **List pending candidates:** run `"$PY" "$ENGINE/review.py" list`. This returns
   JSON with `id`, `name`, `title`, `description`, `confidence`, `utility`,
   `composite`, and `trace_outcome` for each. If empty, tell the user there's
   nothing to review and optionally offer to run the miner now
   (`"$PY" "$ENGINE/extractor.py"`).

2. **Present them** to the user as a concise ranked list (highest `composite`
   first): title, one-line description, and the scores like
   `confidence 88% · utility 77% · trace: success`. Briefly note what each score
   means the first time.

3. **For the one(s) the user wants to look at,** run
   `"$PY" "$ENGINE/review.py" show <id>` and show the full `body` (the procedure),
   `trigger`, and `outcome_reason`.

4. **Take the user's decision** for each candidate:
   - **Install (optionally with edits):** if the user wants changes, write a JSON
     file with only the changed fields (any of `name`, `title`, `description`,
     `trigger`, `body`, `tags`) to a temp path, then run
     `"$PY" "$ENGINE/review.py" install <id> --edits /tmp/edits.json --comment "<why>"`.
     With no edits, drop `--edits`. This writes
     `~/.claude/skills/<name>/SKILL.md` so it becomes a live skill.
   - **Reject:** run
     `"$PY" "$ENGINE/review.py" reject <id> --comment "<why they passed>"`.
     Always try to capture a short reason — it's the training signal for future
     mining. The candidate stays in the scratch store; it is not deleted.

5. **Confirm** what happened (installed path, or rejection recorded) and, if the
   user installed a skill, remind them it's available immediately in new
   sessions.

## Notes
- Never delete candidates. Reject keeps them; install promotes them. This is by design.
- If the user asks to see *everything* mined (not just pending), use `list --all`.
- The same actions are available via the VS Code popup ("Discovered Skills"
  panel); this command is the terminal equivalent and stays in sync with it.
