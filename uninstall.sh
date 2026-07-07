#!/usr/bin/env bash
# Remove all installed components. Keeps mined state (candidates/decisions) by
# default so nothing is lost; pass --purge to also delete the state store.
set -euo pipefail

CLAUDE_DIR="$HOME/.claude"
STATE_ROOT="$CLAUDE_DIR/skill-extractor"
PLIST_LABEL="ai.surenode.skill-extractor"
PLIST="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
EXT_DIR="$HOME/.vscode/extensions/claude-skill-extractor"
PY="$(command -v python3 || echo /opt/homebrew/bin/python3)"

launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST" && echo "  ✓ removed launchd timer"
rm -rf "$EXT_DIR" && echo "  ✓ removed VS Code extension (Reload Window to finish)"
rm -rf "$CLAUDE_DIR/skills/review-skills" && echo "  ✓ removed /review-skills skill"

# Remove the SessionStart hook entry.
"$PY" - "$CLAUDE_DIR/settings.json" <<'PYEOF'
import json, sys
p = sys.argv[1]
try:
    s = json.load(open(p))
except Exception:
    sys.exit(0)
ss = s.get("hooks", {}).get("SessionStart", [])
ss = [e for e in ss if not any("session_start.py" in h.get("command","") for h in e.get("hooks", []))]
if ss:
    s["hooks"]["SessionStart"] = ss
elif "hooks" in s and "SessionStart" in s["hooks"]:
    del s["hooks"]["SessionStart"]
json.dump(s, open(p,"w"), indent=2)
print("  ✓ removed SessionStart hook")
PYEOF

if [[ "${1:-}" == "--purge" ]]; then
  rm -rf "$STATE_ROOT" && echo "  ✓ purged state store ($STATE_ROOT)"
else
  echo "  • kept state store at $STATE_ROOT (rerun with --purge to delete mined candidates/decisions)"
fi
echo "✅ Uninstalled."
