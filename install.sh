#!/usr/bin/env bash
# Skill Extractor installer. Idempotent — safe to re-run after edits.
set -euo pipefail
umask 077   # everything this installer creates is private to the user

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$REPO/engine"
PY="$(command -v python3 || echo /opt/homebrew/bin/python3)"
CLAUDE_DIR="$HOME/.claude"
STATE_ROOT="$CLAUDE_DIR/skill-extractor"
STATE_DIR="$STATE_ROOT/state"
SKILLS_DIR="$CLAUDE_DIR/skills"
LA_DIR="$HOME/Library/LaunchAgents"
EXT_DIR="$HOME/.vscode/extensions/claude-skill-extractor"
PLIST_LABEL="ai.surenode.skill-extractor"
PLIST="$LA_DIR/$PLIST_LABEL.plist"
INTERVAL="${SKILL_EXTRACTOR_INTERVAL:-1800}"   # seconds between periodic runs (default 30m)

echo "▸ Skill Extractor install"
echo "  repo:   $REPO"
echo "  python: $PY"

# 1) State dirs -------------------------------------------------------------
mkdir -p "$STATE_DIR" "$STATE_ROOT/logs" "$SKILLS_DIR" "$LA_DIR"
chmod 700 "$STATE_ROOT" "$STATE_DIR" "$STATE_ROOT/logs"

# 2) Extension config (read by the VS Code extension & the review skill) -----
cat > "$STATE_ROOT/extension-config.json" <<JSON
{
  "python": "$PY",
  "engineDir": "$ENGINE",
  "repo": "$REPO"
}
JSON
chmod 600 "$STATE_ROOT/extension-config.json"
echo "  ✓ wrote extension-config.json"

# 3) Install the /review-skills skill ---------------------------------------
rm -rf "$SKILLS_DIR/review-skills"
cp -R "$REPO/skills/review-skills" "$SKILLS_DIR/review-skills"
echo "  ✓ installed /review-skills skill"

# 4) Install the VS Code extension (copy; no build needed) -------------------
rm -rf "$EXT_DIR"
mkdir -p "$EXT_DIR"
cp "$REPO/vscode-extension/package.json" "$REPO/vscode-extension/extension.js" "$EXT_DIR/"
echo "  ✓ installed VS Code extension -> $EXT_DIR (Reload Window to activate)"

# 5) launchd periodic timer --------------------------------------------------
# Generated via plistlib (not string interpolation): paths and the interval are
# data, so XML metacharacters in a path can neither break nor extend the plist,
# and the interval is validated as a positive integer.
"$PY" - "$PLIST" "$PLIST_LABEL" "$PY" "$ENGINE/extractor.py" "$STATE_ROOT" "$INTERVAL" "$HOME" <<'PYEOF'
import plistlib, sys
plist, label, py, extractor, state_root, interval, home = sys.argv[1:8]
try:
    interval = int(interval)
    if interval <= 0:
        raise ValueError
except ValueError:
    sys.exit(f"SKILL_EXTRACTOR_INTERVAL must be a positive integer, got: {interval!r}")
data = {
    "Label": label,
    "ProgramArguments": [py, extractor],
    "StartInterval": interval,
    "RunAtLoad": True,
    "StandardOutPath": f"{state_root}/logs/launchd.out.log",
    "StandardErrorPath": f"{state_root}/logs/launchd.err.log",
    "EnvironmentVariables": {
        "PATH": f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    },
}
with open(plist, "wb") as fh:
    plistlib.dump(data, fh)
PYEOF

# Logs can carry transcript-derived error text: create them private BEFORE
# launchd or the initial background run can create them with a wider mode.
touch "$STATE_ROOT/logs/launchd.out.log" "$STATE_ROOT/logs/launchd.err.log" "$STATE_ROOT/logs/run.log"
chmod 600 "$STATE_ROOT/logs/launchd.out.log" "$STATE_ROOT/logs/launchd.err.log" "$STATE_ROOT/logs/run.log"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "  ✓ loaded launchd timer ($PLIST_LABEL, every ${INTERVAL}s)"

# 6) SessionStart hook in ~/.claude/settings.json ----------------------------
"$PY" - "$CLAUDE_DIR/settings.json" "$ENGINE/../hooks/session_start.py" "$PY" <<'PYEOF'
import json, os, sys
settings_path, hook_script, py = sys.argv[1], os.path.abspath(sys.argv[2]), sys.argv[3]
try:
    with open(settings_path) as f: s = json.load(f)
except Exception:
    s = {}
hooks = s.setdefault("hooks", {})
ss = hooks.setdefault("SessionStart", [])
cmd = f'{py} "{hook_script}"'
# Remove any prior skill-extractor hook, then add ours (idempotent).
def is_ours(entry):
    for h in entry.get("hooks", []):
        if "session_start.py" in h.get("command", ""):
            return True
    return False
ss = [e for e in ss if not is_ours(e)]
ss.append({"hooks": [{"type": "command", "command": cmd}]})
hooks["SessionStart"] = ss
with open(settings_path, "w") as f: json.dump(s, f, indent=2)
print("  ✓ wired SessionStart hook into", settings_path)
PYEOF

# 7) Kick an initial mining pass in the background (launchd RunAtLoad also fires one)
echo "▸ Starting an initial mining pass in the background…"
nohup "$PY" "$ENGINE/extractor.py" >>"$STATE_ROOT/logs/run.log" 2>&1 &

echo
echo "✅ Installed. Next steps:"
echo "   • In VS Code: Cmd+Shift+P → 'Developer: Reload Window' to activate the extension."
echo "   • Review anytime: run /review-skills in Claude Code, or click the '🎓 Skills' status-bar item."
echo "   • Status: $PY $ENGINE/extractor.py --status"
