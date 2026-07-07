"""
Skill Extractor — shared library.

Responsibilities:
  * Locate & incrementally read Claude Code transcripts (JSONL) across projects.
  * Reconstruct a linear conversation from the raw records.
  * Segment a session into task-sized chunks (bounded by user prompts).
  * Compute heuristic "outcome" signals for a segment (success / meh / failure).
  * Drive the local `claude` CLI headlessly to mine candidate skills.
  * Score candidates (confidence + utility) by blending LLM self-assessment
    with the trace's measured outcome.
  * Read/write all persistent state (candidates, decisions, pending, cursor).

No third-party dependencies — stdlib only. Python 3.9+.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------- #
# Paths & configuration
# --------------------------------------------------------------------------- #

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
SKILLS_DIR = os.path.join(CLAUDE_DIR, "skills")            # install target for approved skills

# Runtime state lives outside the repo so it survives repo moves / reclones.
STATE_ROOT = os.path.join(CLAUDE_DIR, "skill-extractor")
STATE_DIR = os.path.join(STATE_ROOT, "state")
LOG_DIR = os.path.join(STATE_ROOT, "logs")

CANDIDATES_FILE = os.path.join(STATE_DIR, "candidates.jsonl")   # append-only scratch of EVERY mined candidate
DECISIONS_FILE = os.path.join(STATE_DIR, "decisions.jsonl")     # user install/reject decisions + comments
PENDING_FILE = os.path.join(STATE_DIR, "pending.json")          # candidates awaiting review (drives the popup)
CURSOR_FILE = os.path.join(STATE_DIR, "cursor.json")            # per-transcript byte offsets for incremental reads
MINED_SEGMENTS_FILE = os.path.join(STATE_DIR, "mined_segments.json")  # fingerprints of already-mined segments
LOG_FILE = os.path.join(LOG_DIR, "run.log")

# Repo root = parent of this file's directory (engine/..).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(REPO_ROOT, "config.json")

CLAUDE_BIN = shutil.which("claude") or os.path.join(HOME, ".local", "bin", "claude")

DEFAULT_CONFIG = {
    "model": "sonnet",                 # model alias for the mining LLM (cheap-ish, strong enough)
    "scope": "all",                    # "all" | list of project-slug substrings to include
    "min_confidence": 0.55,            # candidates below this are stored but not surfaced for review
    "min_utility": 0.40,
    "min_segment_chars": 700,          # skip trivially short segments
    "max_segment_chars": 22000,        # truncate very long segments before sending to the miner
    "max_segments_per_run": 8,         # cost cap: LLM calls per extractor run
    "mine_timeout_sec": 240,
    "exclude_projects": [],            # project-slug substrings to skip entirely
    "min_tool_calls": 2,               # a segment needs at least this much "doing" to be worth mining
    "surface_threshold": 0.55,         # composite score needed to move a candidate into `pending`
    "redact_secrets": True,            # redact key/token/PII patterns from segments BEFORE the mining LLM sees them
    "ack_command_backend": False,      # explicit opt-in required: the command backend sends transcript excerpts to the command you configure
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE) as fh:
            cfg.update(json.load(fh))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg


def ensure_dirs() -> None:
    for d in (STATE_DIR, LOG_DIR, SKILLS_DIR):
        os.makedirs(d, exist_ok=True)
    # State may contain transcript-derived text; keep it private to the user
    # even under a permissive umask.
    for d in (STATE_ROOT, STATE_DIR, LOG_DIR):
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass


def _append_private(path: str, line: str) -> None:
    """Append one line, creating the file 0600 (state can hold sensitive text)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8", errors="replace"))
    finally:
        os.close(fd)


def macos_notify(title: str, message: str) -> None:
    """Fire a native macOS notification (best-effort). Lets the user know skills
    were discovered the moment the miner surfaces them, regardless of whether the
    VS Code extension is active/focused."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{esc(message)}" with title "{esc(title)}" sound name "Glass"'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception:
        pass


def log(msg: str) -> None:
    ensure_dirs()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    _append_private(LOG_FILE, line)
    print(line)


# --------------------------------------------------------------------------- #
# Transcript discovery & incremental reads
# --------------------------------------------------------------------------- #

def iter_transcripts(cfg: dict) -> list[str]:
    """All transcript .jsonl paths in scope, respecting include/exclude config."""
    paths = sorted(glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")))
    scope = cfg.get("scope", "all")
    excl = cfg.get("exclude_projects", []) or []
    out = []
    for p in paths:
        slug = os.path.basename(os.path.dirname(p))
        if any(e and e in slug for e in excl):
            continue
        if scope != "all" and isinstance(scope, list):
            if not any(s in slug for s in scope):
                continue
        out.append(p)
    return out


def read_new_lines(path: str, offset: int) -> tuple[list[dict], int]:
    """Read JSONL records appended since `offset` (a byte position). Returns (records, new_offset).

    If the file shrank (rotated/truncated) we restart from 0.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    if size < offset:
        offset = 0
    records: list[dict] = []
    with open(path, "r", errors="replace") as fh:
        fh.seek(offset)
        data = fh.read()
        new_offset = fh.tell()
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records, new_offset


# --------------------------------------------------------------------------- #
# Conversation reconstruction
# --------------------------------------------------------------------------- #

@dataclass
class Event:
    role: str                 # "user" | "assistant" | "tool_result"
    text: str = ""
    tool: str = ""            # tool name for tool_use / tool_result
    tool_input: str = ""
    is_error: bool = False
    ts: str = ""


def _blocks(content: Any) -> list[dict]:
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def reconstruct(records: Iterable[dict]) -> list[Event]:
    """Flatten raw transcript records into an ordered list of Events."""
    events: list[Event] = []
    for r in records:
        rtype = r.get("type")
        msg = r.get("message") or {}
        ts = r.get("timestamp", "")
        if rtype == "user":
            content = msg.get("content")
            if isinstance(content, str):
                # Skip harness-injected system reminders / task notifications for signal purposes,
                # but keep genuine user prompts.
                events.append(Event(role="user", text=content, ts=ts))
            else:
                for b in _blocks(content):
                    if b.get("type") == "tool_result":
                        c = b.get("content")
                        txt = c if isinstance(c, str) else json.dumps(c)[:4000]
                        events.append(Event(
                            role="tool_result",
                            text=txt or "",
                            is_error=bool(b.get("is_error")),
                            ts=ts,
                        ))
                    elif b.get("type") == "text":
                        events.append(Event(role="user", text=b.get("text", ""), ts=ts))
        elif rtype == "assistant":
            for b in _blocks(msg.get("content")):
                bt = b.get("type")
                if bt == "text":
                    events.append(Event(role="assistant", text=b.get("text", ""), ts=ts))
                elif bt == "tool_use":
                    events.append(Event(
                        role="assistant",
                        tool=b.get("name", ""),
                        tool_input=json.dumps(b.get("input", {}))[:2000],
                        ts=ts,
                    ))
                # thinking blocks intentionally ignored
    return events


# Harness-injected user text we don't count as real user sentiment.
_SYNTHETIC_USER = re.compile(
    r"^\s*<(system-reminder|task-notification|command-name|local-command|ide_selection|task-id)",
    re.IGNORECASE,
)


def is_real_user_text(ev: Event) -> bool:
    return ev.role == "user" and bool(ev.text.strip()) and not _SYNTHETIC_USER.match(ev.text)


# --------------------------------------------------------------------------- #
# Segmentation
# --------------------------------------------------------------------------- #

@dataclass
class Segment:
    project: str
    session_id: str
    index: int
    events: list[Event] = field(default_factory=list)
    # The opening user message of the NEXT segment — this is where feedback about
    # THIS segment's work ("thanks, that worked" / "no, still broken") usually lands.
    feedback: str = ""

    def char_len(self) -> int:
        return sum(len(e.text) + len(e.tool_input) for e in self.events)

    def tool_calls(self) -> int:
        return sum(1 for e in self.events if e.role == "assistant" and e.tool)

    def render(self, max_chars: int) -> str:
        """Compact transcript text for the miner prompt."""
        parts: list[str] = []
        for e in self.events:
            if e.role == "user" and is_real_user_text(e):
                parts.append(f"USER: {e.text.strip()[:1500]}")
            elif e.role == "assistant" and e.tool:
                parts.append(f"ASSISTANT→tool[{e.tool}]: {e.tool_input[:600]}")
            elif e.role == "assistant" and e.text.strip():
                parts.append(f"ASSISTANT: {e.text.strip()[:1200]}")
            elif e.role == "tool_result":
                tag = "ERROR" if e.is_error else "ok"
                parts.append(f"RESULT[{tag}]: {e.text.strip()[:500]}")
        text = "\n".join(parts)
        if len(text) > max_chars:
            head = text[: max_chars // 2]
            tail = text[-max_chars // 2 :]
            text = head + "\n…[trimmed]…\n" + tail
        if self.feedback:
            text += f"\n\n[USER FEEDBACK AFTER THIS TASK]: {self.feedback.strip()[:600]}"
        return text


def segment_session(project: str, session_id: str, events: list[Event],
                    cfg: dict) -> list[Segment]:
    """Split a session into task-sized segments, delimited by genuine user prompts.

    A new real user prompt starts a new segment, so each segment is roughly one
    task/goal — the natural unit for a reusable procedure.
    """
    max_chars = cfg["max_segment_chars"]
    segments: list[Segment] = []
    cur: list[Event] = []

    def flush():
        if cur:
            segments.append(Segment(project, session_id, len(segments), list(cur)))

    for ev in events:
        if is_real_user_text(ev) and cur:
            # boundary: new task begins
            flush()
            cur = [ev]
        else:
            cur.append(ev)
            # hard size cap — split mid-task if a single task is enormous
            if sum(len(e.text) + len(e.tool_input) for e in cur) > max_chars * 2:
                flush()
                cur = []
    flush()
    # Attribute each segment's trailing feedback = next segment's opening user text.
    for i, seg in enumerate(segments):
        if i + 1 < len(segments):
            nxt = segments[i + 1]
            opener = next((e.text for e in nxt.events if is_real_user_text(e)), "")
            seg.feedback = opener[:600]
    return segments


# Standing-instruction language — the linguistic signature of a way-of-working
# being expressed (often without the user realizing it's abstractable).
_INSTRUCTION_RE = re.compile(
    r"\b(always|never|from now on|going forward|every time|each time|make sure( to| you)?|"
    r"prefer|i want you to|i need you to|remember to|stop (doing|saying)|don'?t ever|"
    r"in the future|whenever you)\b",
    re.IGNORECASE,
)


def worth_mining(seg: Segment, cfg: dict) -> bool:
    """Is this segment worth an LLM call?

    Doing-heavy segments qualify via tool calls. But *conversation-heavy*
    segments with little tool activity can still carry implicit ways-of-working
    (standing instructions, corrections, preferences) — let those through when
    there's enough genuine user text to learn from.
    """
    if seg.tool_calls() >= cfg["min_tool_calls"]:
        return seg.char_len() >= cfg["min_segment_chars"]
    # Conversation-only: worth mining when there's substantial genuine user text
    # OR the user is expressing standing instructions / ways-of-working — prime
    # preference ore even in short exchanges.
    user_texts = [e.text for e in seg.events if is_real_user_text(e)]
    if not user_texts:
        return False
    if sum(len(t) for t in user_texts) >= cfg.get("min_user_chars", 300):
        return True
    return any(_INSTRUCTION_RE.search(t) for t in user_texts)


# --------------------------------------------------------------------------- #
# Outcome heuristics — did the trace using this procedure end well?
# --------------------------------------------------------------------------- #

_POS = re.compile(
    r"\b(thanks|thank you|perfect|great|awesome|works|working|nice|exactly|"
    r"that('?s| is) (it|right|correct)|lgtm|ship it|beautiful|love it|amazing)\b",
    re.IGNORECASE,
)
_NEG = re.compile(
    r"(^\s*no+[.,!]?\s|\bnope|\bwrong|\bincorrect|that('?s| is)? not\b|doesn'?t work|not working|"
    r"still (broken|failing|fails|errors?)|revert|undo|broke|broken|"
    r"that'?s wrong|bad|fix this|why (did|is|are)|didn'?t work|failed)\b",
    re.IGNORECASE,
)


def outcome_signals(seg: Segment) -> dict:
    """Heuristic read of how the segment ended. Returns signals + a 0..1 score
    and a coarse label the miner is also asked to independently judge."""
    tool_total = 0
    tool_err = 0
    pos = 0
    neg = 0
    for e in seg.events:
        if e.role == "tool_result":
            tool_total += 1
            if e.is_error:
                tool_err += 1
        elif is_real_user_text(e):
            if _POS.search(e.text):
                pos += 1
            if _NEG.search(e.text):
                neg += 1

    # Trailing feedback (opener of the next segment) is the strongest outcome signal.
    fb = seg.feedback or ""
    if _POS.search(fb):
        pos += 2
    if _NEG.search(fb):
        neg += 2

    err_rate = (tool_err / tool_total) if tool_total else 0.0

    # Start neutral; nudge by signals.
    score = 0.5
    score -= err_rate * 0.4
    score += min(pos, 3) * 0.12
    score -= min(neg, 3) * 0.18
    # A late user prompt after lots of work with no negativity usually means "moved on happily".
    score = max(0.0, min(1.0, score))

    if score >= 0.62:
        label = "success"
    elif score <= 0.4:
        label = "failure"
    else:
        label = "meh"

    return {
        "tool_calls": tool_total,
        "tool_errors": tool_err,
        "error_rate": round(err_rate, 3),
        "positive_signals": pos,
        "negative_signals": neg,
        "heuristic_score": round(score, 3),
        "heuristic_label": label,
    }


# --------------------------------------------------------------------------- #
# The mining LLM (local `claude` CLI, headless)
# --------------------------------------------------------------------------- #

# Skill taxonomy. "outcome_weight" controls how much the source trace's outcome
# scales utility: a technique from a failed trace is suspect, but a *preference*
# or *guardrail* expressed in a failed trace is just as valid (often more so).
SKILL_CATEGORIES = {
    "technique":  {"desc": "a concrete technical procedure (commands, code patterns, debugging recipes)", "outcome_weight": 0.45},
    "workflow":   {"desc": "a multi-step way of organizing work (ordering, parallelization, checkpoints)", "outcome_weight": 0.40},
    "preference": {"desc": "an implicit way-of-working the USER keeps expressing — standing instructions, repeated corrections, formats they ask for, how they like results communicated. They may not realize it's abstractable.", "outcome_weight": 0.15},
    "guardrail":  {"desc": "a what-to-avoid / corrected-approach learned from something going wrong", "outcome_weight": 0.10},
    "automation": {"desc": "a manual sequence the user repeats that could be one reusable command/skill", "outcome_weight": 0.35},
    "domain":     {"desc": "reusable domain knowledge or heuristics (finance, infra, testing, ...)", "outcome_weight": 0.30},
}

MINE_PROMPT = """\
You are a "skill miner". Below is a transcript segment from a coding-agent \
session (one user task and the agent's work on it). Identify GENERALIZABLE, \
REUSABLE skills demonstrated or expressed in it — things that would make an \
agent faster or more correct on similar future work.

Look for ALL of these categories:
- technique: {cat_technique}
- workflow: {cat_workflow}
- preference: {cat_preference}
- guardrail: {cat_guardrail}
- automation: {cat_automation}
- domain: {cat_domain}

IMPORTANT — hidden skills: pay special attention to IMPLICIT ways-of-working the
user expresses without realizing they could be a skill: recurring instructions
("always/never/prefer/make sure..."), repeated corrections of the agent, habits
in how they want work verified, formatted, or reported, and repeated manual
sequences. These are often the most valuable extractions. State them as
explicit, reusable rules.

Rules:
- Only extract what is genuinely reusable beyond this one task. NO one-off
  actions, trivial single commands, or project-specific trivia.
- Prefer 0 skills over a weak/generic one. Most segments yield 0-2 skills.
- Each skill body must be concrete and actionable — SKILL.md quality.
- Judge the OUTCOME of THIS trace honestly: success, meh, or failure. A skill
  from a failed trace can still be valuable (especially guardrails) — say so.
{learning}
Return ONLY a JSON array (no prose, no markdown fences). Each element exactly:
{
  "name": "kebab-case-slug",
  "title": "Short human title",
  "category": "technique" | "workflow" | "preference" | "guardrail" | "automation" | "domain",
  "description": "One sentence: what it does + when to trigger it (for the SKILL.md frontmatter).",
  "trigger": "When an agent should reach for this skill.",
  "body": "Markdown: the actual procedure/rule as concrete numbered steps or explicit directives.",
  "trace_outcome": "success" | "meh" | "failure",
  "outcome_reason": "Why you judged the trace that way (cite evidence).",
  "llm_confidence": 0.0-1.0,
  "llm_utility": 0.0-1.0,
  "tags": ["..."]
}
If there are no worthwhile skills, return exactly: []

TRANSCRIPT SEGMENT:
---
{segment}
---
Return the JSON array now."""

STRICT_RETRY_SUFFIX = """

REMINDER: Your ENTIRE reply must be a single valid JSON array (or []). No text
before or after it. No markdown fences. Use double quotes for all strings."""


# --------------------------------------------------------------------------- #
# Secret redaction — runs BEFORE any segment text reaches a mining LLM.
# Transcripts routinely contain pasted keys, .env dumps, and tokens in tool
# output; the miner needs the shape of the work, never the secret values.
# On by default (config "redact_secrets": false to disable — not recommended).
# --------------------------------------------------------------------------- #
_REDACTORS: list[tuple[str, "re.Pattern[str]", str]] = [
    ("private-key",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "[redacted:private-key]"),
    ("jwt",
     re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
     "[redacted:jwt]"),
    ("aws-access-key",
     re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
     "[redacted:aws-access-key]"),
    ("api-key",
     re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b"),
     "[redacted:api-key]"),
    ("api-key",
     re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
     "[redacted:api-key]"),
    ("token",
     re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
     "[redacted:token]"),
    ("token",
     re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
     "[redacted:token]"),
    ("token",
     re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._/+=-]{20,}"),
     "Bearer [redacted:token]"),
    ("secret",  # password embedded in a URL: scheme://user:secret@host
     re.compile(r"://([^:@/\s]+):([^@/\s]+)@"),
     r"://\1:[redacted:secret]@"),
    ("env-secret",  # KEY=value lines for obviously-secret variable names
     re.compile(r"(?im)^([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|APIKEY|PRIVATE_KEY)[A-Z0-9_]*\s*=\s*)\S+"),
     r"\1[redacted:env-secret]"),
]


def redact_text(text: str) -> tuple[str, int]:
    """Return (redacted text, number of redactions applied)."""
    total = 0
    for _kind, rx, repl in _REDACTORS:
        text, n = rx.subn(repl, text)
        total += n
    return text, total


def render_mine_prompt(segment_text: str, learning: str) -> str:
    p = MINE_PROMPT
    for cat, meta in SKILL_CATEGORIES.items():
        p = p.replace("{cat_" + cat + "}", meta["desc"])
    p = p.replace("{learning}", ("\n" + learning + "\n") if learning else "")
    return p.replace("{segment}", segment_text)


def _extract_json_array(text: str) -> list:
    """Best-effort: pull the first top-level JSON array out of model output."""
    text = text.strip()
    # strip accidental code fences
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    start = text.find("[")
    if start == -1:
        return []
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    frag = text[start : i + 1]
                    try:
                        return json.loads(frag)
                    except json.JSONDecodeError:
                        return []
    return []


# Per-run usage accumulator, filled by _call_claude from the CLI's result
# envelope. extractor.run() reads/rests this to enforce budgets and write the
# usage ledger.
RUN_USAGE = {"calls": 0, "input_tokens": 0, "output_tokens": 0,
             "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0}

USAGE_FILE = os.path.join(STATE_DIR, "usage.jsonl")


def reset_run_usage() -> None:
    for k in RUN_USAGE:
        RUN_USAGE[k] = 0.0 if k == "cost_usd" else 0


def _record_usage(env_obj: dict) -> None:
    u = env_obj.get("usage") or {}
    RUN_USAGE["calls"] += 1
    RUN_USAGE["input_tokens"] += int(u.get("input_tokens") or 0)
    RUN_USAGE["output_tokens"] += int(u.get("output_tokens") or 0)
    RUN_USAGE["cache_read_tokens"] += int(u.get("cache_read_input_tokens") or 0)
    RUN_USAGE["cache_write_tokens"] += int(u.get("cache_creation_input_tokens") or 0)
    try:
        RUN_USAGE["cost_usd"] += float(env_obj.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        pass


def append_usage_ledger(extra: dict) -> None:
    ensure_dirs()
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **RUN_USAGE, **extra}
    rec["input_tokens"] = int(rec["input_tokens"]); rec["output_tokens"] = int(rec["output_tokens"])
    _append_private(USAGE_FILE, json.dumps(rec))


def usage_totals(since_prefix: str = "") -> dict:
    """Aggregate the ledger; since_prefix like '2026-07-05' limits to that day."""
    tot = {"runs": 0, "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    if not os.path.exists(USAGE_FILE):
        return tot
    with open(USAGE_FILE, errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since_prefix and not str(r.get("ts", "")).startswith(since_prefix):
                continue
            tot["runs"] += 1
            tot["calls"] += int(r.get("calls") or 0)
            tot["input_tokens"] += int(r.get("input_tokens") or 0)
            tot["output_tokens"] += int(r.get("output_tokens") or 0)
            tot["cost_usd"] += float(r.get("cost_usd") or 0.0)
    tot["cost_usd"] = round(tot["cost_usd"], 4)
    return tot


def _call_command_backend(prompt: str, cfg: dict) -> Optional[str]:
    """Generic mining backend: run any LLM CLI (`mining_command` in config),
    prompt on stdin, model text on stdout. Makes the miner work with Codex
    (`codex exec ... -`), `llm`, `ollama run`, or anything else scriptable.

    Safety properties:
    - Requires explicit opt-in (config ``ack_command_backend: true``): the
      prompt contains transcript excerpts, and this backend sends them to
      whatever command you configure.
    - The command runs WITHOUT a shell. Configure it as an argv list
      (recommended) or a plain string split with shlex; pipes/redirects are
      not supported — wrap them in a script if you need them.
    - No token accounting (the CLI's envelope format is unknown): the call is
      still counted so budgets-by-calls keep working, but ``max_usd_per_day``
      cannot see costs on this backend — ``max_segments_per_run`` is the cap.
    """
    command = cfg.get("mining_command")
    if not command:
        return None
    if not cfg.get("ack_command_backend", False):
        log("  command backend refused: transcript excerpts would be sent to "
            f"{command!r}. Set \"ack_command_backend\": true in config.json to "
            "acknowledge this and enable it.")
        return None
    if isinstance(command, str):
        import shlex
        argv = shlex.split(command)
    else:
        argv = [str(a) for a in command]
    if not argv:
        return None
    try:
        proc = subprocess.run(
            argv, shell=False, input=prompt,
            capture_output=True, text=True,
            timeout=cfg.get("mine_timeout_sec", 240),
            cwd=STATE_ROOT if os.path.isdir(STATE_ROOT) else HOME,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log(f"  miner failed (command backend): {exc!r}")
        return None
    if proc.returncode != 0:
        log(f"  miner exit {proc.returncode} (command backend): {proc.stderr[:300]}")
        return None
    RUN_USAGE["calls"] += 1
    return proc.stdout.strip()


def _call_llm(prompt: str, model: str, cfg: dict) -> Optional[str]:
    """Backend dispatch: 'claude_cli' (default) or 'command'."""
    if cfg.get("mining_backend", "claude_cli") == "command":
        return _call_command_backend(prompt, cfg)
    return _call_claude(prompt, model, cfg)


def _call_claude(prompt: str, model: str, cfg: dict) -> Optional[str]:
    """One headless claude CLI call. Returns the result text, or None on
    process-level failure (so callers can try another model)."""
    cmd = [
        CLAUDE_BIN,
        "-p",
        "--model", model,
        "--output-format", "json",
        "--no-session-persistence",
    ]
    env = dict(os.environ)
    env.setdefault("PATH", "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin")
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=cfg.get("mine_timeout_sec", 240),
            env=env,
            cwd=STATE_ROOT if os.path.isdir(STATE_ROOT) else HOME,
        )
    except subprocess.TimeoutExpired:
        log(f"  miner timeout (model={model})")
        return None
    if proc.returncode != 0:
        log(f"  miner exit {proc.returncode} (model={model}): {proc.stderr[:300]}")
        return None
    raw = proc.stdout.strip()
    # --output-format json wraps the result: {"type":"result","result":"<text>",
    # "usage":{...}, "total_cost_usd":...}. Harvest usage for budget calibration.
    try:
        env_obj = json.loads(raw)
        if isinstance(env_obj, dict) and "result" in env_obj:
            _record_usage(env_obj)
            return str(env_obj["result"])
    except json.JSONDecodeError:
        pass
    RUN_USAGE["calls"] += 1  # count the call even if the envelope was unparseable
    return raw


def model_chain(cfg: dict) -> list[str]:
    """Configured model first, then fallbacks (deduped, order-preserving).
    Best-effort across whatever models this account can use."""
    chain = [str(cfg.get("model", "sonnet"))]
    for m in cfg.get("model_fallbacks", ["haiku"]):
        if m and m not in chain:
            chain.append(str(m))
    return chain


def mine_segment(seg: Segment, cfg: dict, learning: str = "") -> list[dict]:
    """Mine skills from a segment — best-effort across the model chain.

    For each model: one normal attempt; if the output exists but doesn't parse
    as JSON (common with smaller models), one strict retry. Process-level
    failures (model unavailable / overloaded / timeout) fall through to the
    next model in the chain.
    """
    segment_text = seg.render(cfg["max_segment_chars"])
    if cfg.get("redact_secrets", True):
        segment_text, n_redacted = redact_text(segment_text)
        if n_redacted:
            log(f"  redacted {n_redacted} secret-like value(s) before mining")
    prompt = render_mine_prompt(segment_text, learning)
    for model in model_chain(cfg):
        out = _call_llm(prompt, model, cfg)
        if out is None:
            continue  # process failure -> next model
        arr = _extract_json_array(out)
        if arr:
            return arr
        if "[]" in out.replace(" ", ""):
            return []  # genuine "no skills here"
        # Parseable output missing: strict retry once on this model.
        log(f"  unparseable miner output (model={model}); strict retry")
        out2 = _call_llm(prompt + STRICT_RETRY_SUFFIX, model, cfg)
        if out2 is not None:
            arr = _extract_json_array(out2)
            if arr or "[]" in out2.replace(" ", ""):
                return arr
    return []


# --------------------------------------------------------------------------- #
# Learning loop — turn the user's install/reject decisions into (a) calibration
# examples injected into the mining prompt and (b) score priors.
# --------------------------------------------------------------------------- #

def _decided_candidates() -> list[tuple[dict, dict]]:
    """[(decision, candidate)] for every decision whose candidate we can find."""
    by_id = {c.get("id"): c for c in read_candidates()}
    out = []
    for d in read_decisions():
        c = by_id.get(d.get("id"))
        if c:
            out.append((d, c))
    return out


def build_learning_context(max_each: int = 6) -> str:
    """Few-shot calibration block for the mining prompt: what this user installs
    vs rejects, with their stated reasons. Empty string until decisions exist."""
    pairs = _decided_candidates()
    if not pairs:
        return ""
    approved = [(d, c) for d, c in pairs if d.get("action") == "install"][-max_each:]
    rejected = [(d, c) for d, c in pairs if d.get("action") == "reject"][-max_each:]
    lines = ["CALIBRATION — this user has already reviewed mined skills. Learn their taste:"]
    for d, c in approved:
        line = f'- INSTALLED: "{c.get("title")}" ({c.get("category", "technique")})'
        if d.get("comment"):
            line += f' — user said: "{d["comment"]}"'
        lines.append(line)
    for d, c in rejected:
        line = f'- REJECTED: "{c.get("title")}" ({c.get("category", "technique")})'
        if d.get("comment"):
            line += f' — user said: "{d["comment"]}"'
        lines.append(line)
    lines.append(
        "Propose more skills like the INSTALLED ones; do not propose skills like the "
        "REJECTED ones (or anything their rejection reasons would also rule out)."
    )
    return "\n".join(lines)


def build_known_skills_block(max_items: int = 60) -> str:
    """Names of already-surfaced/installed skills, injected into the mining
    prompt so the model doesn't burn output on re-extracting what we have."""
    names: list[str] = []
    for c in read_candidates():
        if c.get("surfaced"):
            names.append(str(c.get("title") or c.get("name")))
    names.extend(sorted(installed_skill_names()))
    seen: set[str] = set()
    uniq = [n for n in names if n.lower() not in seen and not seen.add(n.lower())]
    if not uniq:
        return ""
    recent = uniq[-max_items:]
    return ("ALREADY-KNOWN SKILLS — these are already extracted. Do NOT propose "
            "them again or near-duplicates of them; only genuinely NEW skills:\n"
            + "\n".join(f"- {n}" for n in recent))


def learned_priors() -> dict:
    """Statistical priors from decisions: per-category approval rates and
    tag-affinity counters. Used to nudge scores toward the user's demonstrated
    taste. Neutral (0.5 everywhere) until decisions accumulate."""
    pairs = _decided_candidates()
    cat_stats: dict[str, list[int]] = {}       # cat -> [installs, total]
    approved_tags: dict[str, int] = {}
    rejected_tags: dict[str, int] = {}
    for d, c in pairs:
        cat = c.get("category", "technique")
        st = cat_stats.setdefault(cat, [0, 0])
        st[1] += 1
        bucket = approved_tags if d.get("action") == "install" else rejected_tags
        if d.get("action") == "install":
            st[0] += 1
        for t in (c.get("tags") or []):
            t = str(t).lower()
            bucket[t] = bucket.get(t, 0) + 1
    return {
        # Laplace-smoothed: 0 decisions in a category -> 0.5 (neutral)
        "cat_rate": {cat: (i + 1) / (t + 2) for cat, (i, t) in cat_stats.items()},
        "approved_tags": approved_tags,
        "rejected_tags": rejected_tags,
        "n_decisions": len(pairs),
    }


def _tag_affinity(tags: list, priors: dict) -> float:
    """-1..+1: does this candidate's tag set look like what the user installs
    or what they reject?"""
    if not tags:
        return 0.0
    app = priors.get("approved_tags", {})
    rej = priors.get("rejected_tags", {})
    a = sum(app.get(str(t).lower(), 0) for t in tags)
    r = sum(rej.get(str(t).lower(), 0) for t in tags)
    if a + r == 0:
        return 0.0
    return (a - r) / (a + r)


# --------------------------------------------------------------------------- #
# Scoring — blend LLM self-assessment with measured trace outcome
# --------------------------------------------------------------------------- #

def score_candidate(cand: dict, signals: dict, priors: Optional[dict] = None) -> dict:
    """Produce final confidence & utility in [0,1] and a composite score.

    confidence: is this a real, generalizable skill?  (LLM + evidence)
    utility:    how valuable if reused?  (LLM + trace outcome + learned taste)

    Category-aware: a *technique* from a failed trace is downweighted, but a
    *preference*/*guardrail* expressed in a failed trace is just as valid.
    Learning: once the user has made decisions, per-category approval rates and
    tag affinity nudge scores toward their demonstrated taste (bounded, grows
    with evidence).
    """
    llm_conf = _clamp(cand.get("llm_confidence", 0.5))
    llm_util = _clamp(cand.get("llm_utility", 0.5))
    cat = str(cand.get("category", "technique")).lower()
    cat_meta = SKILL_CATEGORIES.get(cat, SKILL_CATEGORIES["technique"])

    heur = signals["heuristic_score"]                  # 0..1
    model_outcome = str(cand.get("trace_outcome", "meh")).lower()
    outcome_map = {"success": 0.85, "meh": 0.5, "failure": 0.2}
    outcome = 0.5 * heur + 0.5 * outcome_map.get(model_outcome, 0.5)

    # Confidence: mostly the LLM's read, tempered by evidence. For preference/
    # guardrail skills the "evidence" is user text, not tool activity, so the
    # tool bonus applies only to doing-heavy categories.
    evidence_bonus = 0.0
    if cat in ("technique", "workflow", "automation"):
        evidence_bonus = min(signals["tool_calls"], 8) / 8 * 0.1
    confidence = _clamp(0.8 * llm_conf + evidence_bonus + 0.1 * (1 - signals["error_rate"]))

    # Utility: LLM utility scaled by outcome, with category-specific weight.
    ow = cat_meta["outcome_weight"]
    utility = _clamp(llm_util * ((1 - ow) + ow * outcome))

    # Learned taste adjustment (only once decisions exist; influence grows with
    # evidence, capped at 30%).
    learned = None
    if priors and priors.get("n_decisions", 0) > 0:
        cat_rate = priors.get("cat_rate", {}).get(cat, 0.5)       # 0..1
        affinity = _tag_affinity(cand.get("tags") or [], priors)  # -1..+1
        learned = _clamp(0.6 * cat_rate + 0.4 * (0.5 + 0.5 * affinity))
        w = min(0.30, 0.05 * priors["n_decisions"])
        utility = _clamp((1 - w) * utility + w * learned)
        confidence = _clamp((1 - w / 2) * confidence + (w / 2) * learned)

    composite = round(0.5 * confidence + 0.5 * utility, 3)
    out = {
        "confidence": round(confidence, 3),
        "utility": round(utility, 3),
        "outcome_quality": round(outcome, 3),
        "composite": composite,
    }
    if learned is not None:
        out["learned_prior"] = round(learned, 3)
    return out


def _clamp(x: Any, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.5
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------- #
# State I/O
# --------------------------------------------------------------------------- #

def load_cursor() -> dict:
    try:
        with open(CURSOR_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cursor(cursor: dict) -> None:
    ensure_dirs()
    _atomic_write(CURSOR_FILE, json.dumps(cursor, indent=0))


def segment_fingerprint(seg: "Segment") -> str:
    """Content hash of a segment's rendered text. Stable across re-reads, so we
    never spend LLM budget mining the same segment twice — even when the byte
    cursor hasn't advanced yet on a huge, still-growing transcript."""
    return hashlib.sha1(seg.render(200000).encode("utf-8", "replace")).hexdigest()[:20]


def load_mined_segments() -> set[str]:
    try:
        with open(MINED_SEGMENTS_FILE) as fh:
            return set(json.load(fh))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_mined_segments(fps: set[str]) -> None:
    ensure_dirs()
    # keep it bounded — retain the most recent 20k fingerprints
    lst = list(fps)[-20000:]
    _atomic_write(MINED_SEGMENTS_FILE, json.dumps(lst))


def candidate_key(cand: dict) -> str:
    """Dedup key: normalized name + description. Stable across runs."""
    base = (cand.get("name", "") + "|" + cand.get("description", "")).lower()
    base = re.sub(r"[^a-z0-9| ]+", "", base)
    return hashlib.sha1(base.encode()).hexdigest()[:16]


def existing_candidate_keys() -> set[str]:
    keys: set[str] = set()
    for c in read_candidates():
        keys.add(c.get("key", ""))
    return keys


def installed_skill_names() -> set[str]:
    if not os.path.isdir(SKILLS_DIR):
        return set()
    return {d for d in os.listdir(SKILLS_DIR)
            if os.path.isfile(os.path.join(SKILLS_DIR, d, "SKILL.md"))}


def append_candidate(cand: dict) -> None:
    ensure_dirs()
    _append_private(CANDIDATES_FILE, json.dumps(cand))


def read_candidates() -> list[dict]:
    out: list[dict] = []
    if not os.path.exists(CANDIDATES_FILE):
        return out
    with open(CANDIDATES_FILE, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def read_decisions() -> list[dict]:
    out: list[dict] = []
    if not os.path.exists(DECISIONS_FILE):
        return out
    with open(DECISIONS_FILE, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def append_decision(decision: dict) -> None:
    ensure_dirs()
    _append_private(DECISIONS_FILE, json.dumps(decision))


def decided_ids() -> set[str]:
    return {d.get("id", "") for d in read_decisions()}


def load_pending() -> list[dict]:
    try:
        with open(PENDING_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_pending(items: list[dict]) -> None:
    ensure_dirs()
    _atomic_write(PENDING_FILE, json.dumps(items, indent=2))


_STOP_TOKENS = {"the", "a", "an", "with", "and", "for", "of", "in", "to", "on", "via"}


def _tokset(text: str) -> set[str]:
    toks = set(re.split(r"[^a-z0-9]+", str(text).lower()))
    toks.discard("")
    return toks - _STOP_TOKENS


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _similar(a: dict, b: dict, thresh: float) -> bool:
    """Same pattern mined twice under slightly different names? Compare title
    tokens and name+tag tokens separately (tags would otherwise dilute the
    union and let identical titles slip through) — either match counts."""
    j_title = _jaccard(_tokset(a.get("title", "")), _tokset(b.get("title", "")))
    ja = _tokset(a.get("name", "")) | {str(t).lower() for t in (a.get("tags") or [])}
    jb = _tokset(b.get("name", "")) | {str(t).lower() for t in (b.get("tags") or [])}
    return max(j_title, _jaccard(ja - _STOP_TOKENS, jb - _STOP_TOKENS)) >= thresh


def rebuild_pending() -> list[dict]:
    """Pending = surfaced, undecided candidates — consolidated to a reviewable
    queue: exact-key dedup, then similarity clustering (best of each cluster
    survives), then capped at max_pending. Everything suppressed stays in the
    scratch store untouched — nothing is ever lost."""
    cfg = load_config()
    decided = decided_ids()
    pending = [c for c in read_candidates()
               if c.get("surfaced") and c.get("id") not in decided]
    # exact-key dedup, keeping highest composite
    best: dict[str, dict] = {}
    for c in pending:
        k = c.get("key", c.get("id"))
        if k not in best or c.get("score", {}).get("composite", 0) > best[k].get("score", {}).get("composite", 0):
            best[k] = c
    ranked = sorted(best.values(), key=lambda c: c.get("score", {}).get("composite", 0), reverse=True)
    # greedy similarity clustering: strongest of each cluster survives
    thresh = cfg.get("dedup_similarity", 0.5)
    kept: list[dict] = []
    for c in ranked:
        if not any(_similar(c, k, thresh) for k in kept):
            kept.append(c)
    items = kept[: cfg.get("max_pending", 20)]
    save_pending(items)
    return items


def _atomic_write(path: str, data: str) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data.encode("utf-8", errors="replace"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Skill installation (write a SKILL.md into ~/.claude/skills/<name>/)
# --------------------------------------------------------------------------- #

def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", str(name).lower()).strip("-")
    return s or "mined-skill"


# --------------------------------------------------------------------------- #
# Pre-install risk lint. A mined skill is MODEL OUTPUT that becomes a
# persistent agent instruction; before it is installed, flag instruction
# patterns a reviewer should consciously accept, not skim past. Heuristic by
# design: it surfaces risk, it does not certify safety.
# --------------------------------------------------------------------------- #
_RISK_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("pipe-to-shell",
     re.compile(r"(?i)\b(?:curl|wget)\b[^\n|;&]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b")),
    ("encoded-exec",
     re.compile(r"(?i)\bbase64\b[^\n]*\|\s*(?:ba|z|da)?sh\b|\beval\s*\(\s*atob")),
    ("disables-safety",
     re.compile(r"(?i)--no-verify|--dangerously|--disable-warnings\b|verify=False|"
                r"StrictHostKeyChecking\s*=?\s*no|chmod\s+777|--allow-root")),
    ("credential-access",
     re.compile(r"(?i)(?:~|\$HOME)/\.(?:aws|ssh|gnupg|netrc|npmrc|pypirc|docker/config\.json)|"
                r"\bid_rsa\b|\bcredentials?\.json\b|\.env\b[^\n]*(?:cat|read|print|send|upload)|"
                r"(?:cat|less|head)\s+[^\n]*\.env\b")),
    ("exfiltration",
     re.compile(r"(?i)\b(?:curl|wget|http[s]?://)[^\n]*(?:-d|--data|--upload-file|-F )[^\n]*"
                r"(?:\$\(|`|\$[A-Z_]|token|secret|key|passw)")),
    ("hidden-persistence",
     re.compile(r"(?i)\bcrontab\b|\blaunchctl\s+(?:load|bootstrap)\b|\bschtasks\s+/create\b|"
                r"/etc/rc\.|\.bashrc\b|\.zshrc\b[^\n]*(?:>>|echo)")),
    ("prompt-injection",
     re.compile(r"(?i)ignore\s+(?:all\s+)?(?:previous|prior|earlier)\s+instructions|"
                r"do\s+not\s+(?:tell|inform|mention\s+to)\s+the\s+user")),
    ("broad-destructive",
     re.compile(r"(?i)\brm\s+-rf\s+(?:/|~|\$HOME)\b|\bgit\s+push\s+--force\b[^\n]*\b(?:main|master)\b")),
]


def risk_findings(cand: dict) -> list[str]:
    """Distinct risk labels found across the candidate's instruction fields."""
    text = "\n".join(str(cand.get(k, "")) for k in
                     ("name", "title", "description", "trigger", "body"))
    hits: list[str] = []
    for label, rx in _RISK_PATTERNS:
        if rx.search(text) and label not in hits:
            hits.append(label)
    return hits


def install_skill(cand: dict) -> str:
    """Write the candidate as a SKILL.md. Returns the install path.

    Refuses to write through symlinks: a linked ``<slug>/`` or ``SKILL.md``
    would let a prior local write redirect the install anywhere the user can
    write."""
    ensure_dirs()
    name = slugify(cand.get("name", "mined-skill"))
    dest_dir = os.path.join(SKILLS_DIR, name)
    if os.path.islink(dest_dir):
        raise ValueError(f"refusing to install through symlinked dir: {dest_dir}")
    os.makedirs(dest_dir, exist_ok=True)
    desc = cand.get("description", cand.get("title", name)).replace("\n", " ").strip()
    body = cand.get("body", "").strip()
    trigger = cand.get("trigger", "").strip()
    score = cand.get("score", {})
    front = [
        "---",
        f"name: {name}",
        f"description: {desc}",
        "---",
        "",
        f"# {cand.get('title', name)}",
        "",
    ]
    if trigger:
        front += ["**When to use:** " + trigger, ""]
    front.append(body)
    front += [
        "",
        "---",
        f"*Mined by skill-extractor from {cand.get('source', {}).get('project','?')} "
        f"(confidence {score.get('confidence','?')}, utility {score.get('utility','?')}, "
        f"trace outcome {cand.get('trace_outcome','?')}). Review & edit as needed.*",
    ]
    path = os.path.join(dest_dir, "SKILL.md")
    if os.path.islink(path):
        raise ValueError(f"refusing to write through symlink: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, ("\n".join(front) + "\n").encode("utf-8", errors="replace"))
    finally:
        os.close(fd)
    return path
