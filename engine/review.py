#!/usr/bin/env python3
"""
Skill Extractor — review CLI.

The single source of truth for acting on mined candidates. Used by BOTH the
/review-skills slash command (human-in-the-loop terminal flow) and the VS Code
extension's webview (via child_process). Every path keeps the scratch/installed
records consistent so no candidate is ever lost (requirement 5).

Commands (all emit JSON on stdout unless noted):
  list [--all]                 pending candidates (or all scratch with --all)
  show <id>                    full candidate record
  install <id> [--edits FILE]  install as ~/.claude/skills/<name>/SKILL.md; log decision
  reject  <id> [--comment STR] keep in scratch, log rejection + comment
  edit    <id> --edits FILE    update a candidate in place (edits applied on next install)
  count                        {pending, scratch, installed, decided}
  export-pending               pretty pending payload for the popup

`--edits FILE` is a JSON object with any of: name,title,description,trigger,body,tags.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib  # noqa: E402


def _find(cand_id: str) -> dict | None:
    for c in reversed(lib.read_candidates()):   # latest wins if duplicated
        if c.get("id") == cand_id:
            return c
    return None


def _apply_edits(cand: dict, edits: dict) -> dict:
    for k in ("name", "title", "description", "trigger", "body", "tags"):
        if k in edits and edits[k] is not None:
            cand[k] = edits[k]
    if "name" in edits:
        cand["name"] = lib.slugify(cand["name"])
    return cand


def _load_edits(path: str | None) -> dict:
    if not path:
        return {}
    with open(path) as fh:
        return json.load(fh)


def cmd_list(args) -> None:
    if args.all:
        items = lib.read_candidates()
    else:
        items = lib.rebuild_pending()
    slim = [{
        "id": c["id"],
        "name": c.get("name"),
        "category": c.get("category", "technique"),
        "title": c.get("title"),
        "description": c.get("description"),
        "confidence": c.get("score", {}).get("confidence"),
        "utility": c.get("score", {}).get("utility"),
        "composite": c.get("score", {}).get("composite"),
        "trace_outcome": c.get("trace_outcome"),
        "status": c.get("status"),
        "source": c.get("source"),
        "risk": lib.risk_findings(c),
    } for c in items]
    print(json.dumps(slim, indent=2))


def cmd_show(args) -> None:
    c = _find(args.id)
    print(json.dumps(c or {"error": "not found", "id": args.id}, indent=2))


def cmd_install(args) -> None:
    c = _find(args.id)
    if not c:
        print(json.dumps({"error": "not found", "id": args.id}))
        sys.exit(1)
    c = _apply_edits(dict(c), _load_edits(args.edits))
    # A candidate is model output about to become a persistent agent
    # instruction: risky instruction patterns require an explicit, per-install
    # acknowledgement, not a skimmed click.
    risks = lib.risk_findings(c)
    if risks and not args.acknowledge_risk:
        print(json.dumps({
            "error": "risky",
            "id": c["id"],
            "risk": risks,
            "hint": "review the body, then re-run install with --acknowledge-risk "
                    "to accept these patterns",
        }, indent=2))
        sys.exit(3)
    path = lib.install_skill(c)
    lib.append_decision({
        "id": c["id"],
        "key": c.get("key"),
        "name": c.get("name"),
        "action": "install",
        "path": path,
        "comment": args.comment or "",
        "edited": bool(args.edits),
        "risk": risks,
        "risk_acknowledged": bool(risks),
        "score": c.get("score"),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    lib.rebuild_pending()
    print(json.dumps({"ok": True, "action": "install", "path": path,
                      "name": c["name"], "risk": risks}, indent=2))


def cmd_reject(args) -> None:
    c = _find(args.id)
    if not c:
        print(json.dumps({"error": "not found", "id": args.id}))
        sys.exit(1)
    lib.append_decision({
        "id": c["id"],
        "key": c.get("key"),
        "name": c.get("name"),
        "action": "reject",
        "comment": args.comment or "",
        "score": c.get("score"),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    lib.rebuild_pending()
    print(json.dumps({"ok": True, "action": "reject", "id": c["id"], "comment": args.comment or ""}, indent=2))


def cmd_edit(args) -> None:
    """Persist edits to the scratch record so a later install picks them up."""
    edits = _load_edits(args.edits)
    if not edits:
        print(json.dumps({"error": "no edits provided"}))
        sys.exit(1)
    found = False
    lines = []
    for c in lib.read_candidates():
        if c.get("id") == args.id:
            c = _apply_edits(c, edits)
            c["key"] = lib.candidate_key(c)
            found = True
        lines.append(json.dumps(c))
    if not found:
        print(json.dumps({"error": "not found", "id": args.id}))
        sys.exit(1)
    lib._atomic_write(lib.CANDIDATES_FILE, "\n".join(lines) + "\n")
    lib.rebuild_pending()
    print(json.dumps({"ok": True, "action": "edit", "id": args.id}, indent=2))


def cmd_count(args) -> None:
    print(json.dumps({
        "pending": len(lib.load_pending()),
        "scratch": len(lib.read_candidates()),
        "installed": len(lib.installed_skill_names()),
        "decided": len(lib.read_decisions()),
    }))


def cmd_export_pending(args) -> None:
    items = [{**c, "risk": lib.risk_findings(c)} for c in lib.rebuild_pending()]
    print(json.dumps(items, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list"); p.add_argument("--all", action="store_true"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("show"); p.add_argument("id"); p.set_defaults(fn=cmd_show)
    p = sub.add_parser("install"); p.add_argument("id"); p.add_argument("--edits"); p.add_argument("--comment"); p.add_argument("--acknowledge-risk", action="store_true", help="install even though the risk lint flagged instruction patterns"); p.set_defaults(fn=cmd_install)
    p = sub.add_parser("reject"); p.add_argument("id"); p.add_argument("--comment"); p.set_defaults(fn=cmd_reject)
    p = sub.add_parser("edit"); p.add_argument("id"); p.add_argument("--edits", required=True); p.set_defaults(fn=cmd_edit)
    p = sub.add_parser("count"); p.set_defaults(fn=cmd_count)
    p = sub.add_parser("export-pending"); p.set_defaults(fn=cmd_export_pending)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
