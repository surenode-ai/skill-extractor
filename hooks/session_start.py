#!/usr/bin/env python3
"""
SessionStart hook for the skill-extractor.

If there are skills mined from past traces awaiting review, inject a note so the
assistant proactively tells the user they can review/install them (via the
/review-skills command or the VS Code "Discovered Skills" panel). Emits nothing
when the queue is empty, so it's silent in the common case.

Wired into ~/.claude/settings.json under hooks.SessionStart by install.sh.
"""
import json
import os

PENDING = os.path.join(os.path.expanduser("~"), ".claude", "skill-extractor", "state", "pending.json")


def main() -> None:
    try:
        with open(PENDING) as fh:
            pending = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        pending = []

    if not pending:
        return  # silent

    n = len(pending)
    top = pending[:5]
    lines = [
        f'The skill-extractor has {n} skill candidate{"s" if n != 1 else ""} mined from past '
        f"Claude Code traces awaiting the user's review:",
    ]
    for c in top:
        s = c.get("score", {})
        lines.append(
            f'  • "{c.get("title", c.get("name"))}" '
            f'(confidence {int(100*s.get("confidence",0))}%, utility {int(100*s.get("utility",0))}%, '
            f'trace {c.get("trace_outcome","?")})'
        )
    if n > len(top):
        lines.append(f"  …and {n - len(top)} more.")
    lines.append(
        "At a natural break, briefly let the user know they can review & install these by "
        "running /review-skills or opening the VS Code 'Discovered Skills' panel. Do not "
        "interrupt an in-progress task to do so."
    )

    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
