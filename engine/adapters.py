"""
Trace-source adapters — feed conversations from ANY coding-agent harness into
the mining pipeline.

Each adapter discovers session files and converts their native records into the
canonical record shape (docs/INTEGRATION.md §3):

    {"type": "user",      "message": {"content": "<text>"}}
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "..."},
        {"type": "tool_use", "name": "...", "input": {...}}]}}
    {"type": "user",      "message": {"content": [
        {"type": "tool_result", "is_error": false, "content": "..."}]}}

Built-in adapters:
  claude_code — Claude Code transcripts (~/.claude/projects/*/*.jsonl). Native
                format IS the canonical format; no mapping needed.
  codex       — OpenAI Codex CLI rollouts (~/.codex/sessions/**/*.jsonl).
                Best-effort mapping of message / function_call /
                function_call_output payloads.
  jsonl_dir   — any directory of *.jsonl files ALREADY in canonical shape
                (config: "jsonl_dirs": ["/path"]). The escape hatch for any
                other agent: write a small exporter to this format and point
                the extractor at it.

Adding an adapter: subclass Adapter, implement discover() + map_record(),
register in ADAPTERS.
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass
from typing import Optional

HOME = os.path.expanduser("~")


@dataclass
class Source:
    """One session file belonging to one adapter."""
    adapter: str
    path: str          # unique — also the cursor key
    project: str
    session_id: str


class Adapter:
    name = "base"

    def discover(self, cfg: dict) -> list[Source]:
        raise NotImplementedError

    def map_record(self, raw: dict) -> Optional[dict]:
        """Native record -> canonical record (or None to drop)."""
        return raw

    # Shared incremental reader: returns (canonical_records, new_offset).
    def read(self, source: Source, offset: int) -> tuple[list[dict], int]:
        try:
            size = os.path.getsize(source.path)
        except OSError:
            return [], offset
        if size < offset:
            offset = 0
        with open(source.path, "r", errors="replace") as fh:
            fh.seek(offset)
            data = fh.read()
            new_offset = fh.tell()
        records: list[dict] = []
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            mapped = self.map_record(raw)
            if mapped:
                records.append(mapped)
        return records, new_offset


class ClaudeCodeAdapter(Adapter):
    """Claude Code: native format is canonical — identity mapping."""
    name = "claude_code"
    ROOT = os.path.join(HOME, ".claude", "projects")

    def discover(self, cfg: dict) -> list[Source]:
        out = []
        scope = cfg.get("scope", "all")
        excl = cfg.get("exclude_projects", []) or []
        for p in sorted(glob.glob(os.path.join(self.ROOT, "*", "*.jsonl"))):
            slug = os.path.basename(os.path.dirname(p))
            if any(e and e in slug for e in excl):
                continue
            if scope != "all" and isinstance(scope, list) and not any(s in slug for s in scope):
                continue
            out.append(Source(self.name, p, slug, os.path.splitext(os.path.basename(p))[0]))
        return out


class CodexAdapter(Adapter):
    """OpenAI Codex CLI rollouts (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl).

    Best-effort: Codex line shapes have varied across versions. We handle both
    {"type":"response_item","payload":{...}} envelopes and bare payload lines,
    and payload types message / function_call / function_call_output /
    local_shell_call. Unknown lines are dropped silently.
    """
    name = "codex"
    ROOT = os.path.join(HOME, ".codex", "sessions")

    def discover(self, cfg: dict) -> list[Source]:
        root = cfg.get("codex_sessions_dir") or self.ROOT
        out = []
        for p in sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)):
            sid = os.path.splitext(os.path.basename(p))[0]
            out.append(Source(self.name, p, "codex", sid))
        return out

    @staticmethod
    def _text_of(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict):
                    parts.append(c.get("text") or c.get("content") or "")
                elif isinstance(c, str):
                    parts.append(c)
            return "\n".join(x for x in parts if x)
        return ""

    def map_record(self, raw: dict) -> Optional[dict]:
        payload = raw.get("payload") if raw.get("type") in ("response_item", "event_msg") else raw
        if not isinstance(payload, dict):
            return None
        ptype = payload.get("type", "")

        if ptype == "message":
            text = self._text_of(payload.get("content"))
            if not text.strip():
                return None
            role = payload.get("role", "user")
            if role == "assistant":
                return {"type": "assistant",
                        "message": {"content": [{"type": "text", "text": text}]}}
            # user + system prompts both arrive as role user; keep user only
            if role == "user":
                return {"type": "user", "message": {"content": text}}
            return None

        if ptype in ("function_call", "local_shell_call", "custom_tool_call"):
            name = payload.get("name") or ptype
            args = payload.get("arguments") or payload.get("action") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args[:2000]}
            return {"type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": str(name), "input": args}]}}

        if ptype in ("function_call_output", "custom_tool_call_output"):
            out = payload.get("output", "")
            if isinstance(out, dict):
                out_text = str(out.get("output") or out.get("content") or json.dumps(out))[:4000]
                exit_code = out.get("metadata", {}).get("exit_code", out.get("exit_code"))
            else:
                out_text = str(out)[:4000]
                exit_code = None
                # newer codex embeds JSON in the string
                try:
                    parsed = json.loads(out_text)
                    if isinstance(parsed, dict):
                        exit_code = parsed.get("metadata", {}).get("exit_code", parsed.get("exit_code"))
                        out_text = str(parsed.get("output", out_text))[:4000]
                except (json.JSONDecodeError, TypeError):
                    pass
            is_error = bool(exit_code) if exit_code is not None else False
            return {"type": "user",
                    "message": {"content": [{"type": "tool_result",
                                             "is_error": is_error,
                                             "content": out_text}]}}
        return None


class JsonlDirAdapter(Adapter):
    """Any agent: point config `jsonl_dirs` at directories of *.jsonl files
    already in canonical record shape (one session per file)."""
    name = "jsonl_dir"

    def discover(self, cfg: dict) -> list[Source]:
        out = []
        for d in cfg.get("jsonl_dirs", []) or []:
            d = os.path.expanduser(d)
            for p in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
                sid = os.path.splitext(os.path.basename(p))[0]
                out.append(Source(self.name, p, os.path.basename(d.rstrip("/")), sid))
        return out


ADAPTERS: dict[str, Adapter] = {
    a.name: a for a in (ClaudeCodeAdapter(), CodexAdapter(), JsonlDirAdapter())
}


def discover_sources(cfg: dict) -> list[tuple[Adapter, Source]]:
    """All sources across the adapters enabled in config `sources`
    (default: claude_code only)."""
    enabled = cfg.get("sources", ["claude_code"])
    pairs: list[tuple[Adapter, Source]] = []
    for name in enabled:
        adapter = ADAPTERS.get(name)
        if not adapter:
            continue
        for src in adapter.discover(cfg):
            pairs.append((adapter, src))
    return pairs
