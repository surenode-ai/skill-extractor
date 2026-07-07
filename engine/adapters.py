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
import re
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


def _slugify(name: str) -> str:
    """Local copy of lib.slugify so this module stays import-free of lib."""
    s = re.sub(r"[^a-z0-9-]+", "-", str(name).lower()).strip("-")
    return s or "codex"


# Sessions imported into Codex from another agent flatten that agent's tool
# calls into text like "[external_agent_tool_call: Read]". Mining those would
# double-mine the other agent's transcripts under a codex label.
IMPORTED_MARKER = "[external_agent_tool_call:"

# Codex exec output is plain text ending in "Process exited with code N".
_EXIT_CODE_RE = re.compile(r"Process exited with code\s+(-?\d+)")


class CodexAdapter(Adapter):
    """OpenAI Codex CLI rollouts (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl).

    Best-effort: Codex line shapes have varied across versions. We handle both
    {"type":"response_item","payload":{...}} envelopes and bare payload lines,
    and payload types message / function_call / function_call_output /
    local_shell_call. Unknown lines are dropped silently.

    Three behaviors beyond the basic mapping:
    - **Identity from session_meta.** The project is derived from the session's
      ``cwd`` (slugified) and the session id from its ``id``, so provenance
      matches how other sources are slugged instead of a flat "codex".
    - **Imported sessions are skipped by default.** Sessions imported from
      another agent would be double-mined; detection scans the whole file on
      first read (markers can sit hundreds of records deep) and re-verifies
      with a raw scan whenever an incremental read starts past the file head.
      Opt in with config ``include_imported_codex_sessions: true``.
    - **Exec exit codes count as errors.** Plain-text tool output ending in
      "Process exited with code N" marks the tool_result as an error when
      N != 0, so failed commands feed outcome scoring.
    """
    name = "codex"
    ROOT = os.path.join(HOME, ".codex", "sessions")

    def __init__(self) -> None:
        self._cfg: dict = {}

    @staticmethod
    def _identity(path: str) -> tuple[str, str]:
        """(project, session_id) from the head session_meta record, falling
        back to ("codex", filename-stem)."""
        sid = os.path.splitext(os.path.basename(path))[0]
        project = "codex"
        try:
            with open(path, "r", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i >= 16:  # session_meta is the first record in practice
                        break
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") == "session_meta":
                        payload = rec.get("payload") or {}
                        cwd = str(payload.get("cwd") or "")
                        if cwd:
                            project = _slugify(cwd)
                        sid = str(payload.get("id") or "") or sid
                        break
        except OSError:
            pass
        return project, sid

    def discover(self, cfg: dict) -> list[Source]:
        self._cfg = cfg  # read() needs the imported-session toggle
        root = cfg.get("codex_sessions_dir") or self.ROOT
        scope = cfg.get("scope", "all")
        excl = cfg.get("exclude_projects", []) or []
        out = []
        for p in sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)):
            project, sid = self._identity(p)
            if any(e and e in project for e in excl):
                continue
            if scope != "all" and isinstance(scope, list) and not any(s in project for s in scope):
                continue
            out.append(Source(self.name, p, project, sid))
        return out

    # -- imported-session detection ----------------------------------------- #
    @staticmethod
    def _records_look_imported(records: list) -> bool:
        """The marker lives in message text; mapping preserves it verbatim."""
        for rec in records:
            msg = rec.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                if IMPORTED_MARKER in content:
                    return True
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and IMPORTED_MARKER in str(
                            block.get("text") or block.get("content") or ""):
                        return True
        return False

    @staticmethod
    def _file_contains_marker(path: str) -> bool:
        """Raw substring scan of the whole file (no JSON parsing). Used when an
        incremental read starts past the head, so a marker in the already
        consumed prefix still counts."""
        overlap = len(IMPORTED_MARKER) - 1
        tail = ""
        try:
            with open(path, "r", errors="replace") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        return False
                    if IMPORTED_MARKER in tail + chunk:  # tail catches straddlers
                        return True
                    tail = chunk[-overlap:]
        except OSError:
            return False

    def read(self, source: Source, offset: int) -> tuple[list[dict], int]:
        records, new_offset = super().read(source, offset)
        include_imported = bool(self._cfg.get("include_imported_codex_sessions", False))
        if records and not include_imported and (
                self._records_look_imported(records)
                or (offset > 0 and self._file_contains_marker(source.path))):
            # Skip the session but advance the cursor: an imported file that
            # never grows is then skipped for free on every later run.
            return [], new_offset
        return records, new_offset

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
            if exit_code is None:
                # Exec output is plain text ("...\nProcess exited with code N").
                m = _EXIT_CODE_RE.search(out_text)
                if m:
                    exit_code = int(m.group(1))
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
