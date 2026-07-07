# Contributing

Thanks for helping improve skill-extractor.

## Adapters over features

This project's core promise is a codebase **small enough to actually read**:
a stdlib-only engine you can audit in an afternoon. Every merged feature
spends some of that. So contributions are weighted deliberately:

| Contribution | Reception |
|---|---|
| **New trace-source adapter** (mine another agent's sessions) | The most wanted contribution. ~100 lines + tests, never touches the core |
| **Exporter** to the canonical JSONL shape (INTEGRATION.md §3) | Equally welcome, even smaller (~30 lines), works with the existing `jsonl_dir` adapter |
| **Risk-lint / redaction patterns** (new token formats, new dangerous-instruction shapes) | Welcome: tiny diffs, direct security value, must come with a realistic test sample |
| **Bug fixes** | Always |
| **Features** | High bar. Open an issue first; prefer proposing an extension point over an addition. "It would be nice if the engine also..." usually belongs in a layer on top (see INTEGRATION.md) |

What we will not merge: skill *content* (a directory of community-submitted
skills). Skills are model output that becomes agent instructions; sharing them
safely needs review and governance this repo deliberately does not provide.
Mine your own; share through a governed catalog.

### Request for Adapters (RFA)

Open (or claim) an "Adapter request" issue. Currently wanted:

- **Gemini / Antigravity CLI**
- **opencode**
- **Aider** (`.aider.chat.history.md` — likely an exporter, not an adapter)
- **Cursor** (session storage is an internal SQLite; an exporter is probably
  the realistic shape)

The Codex adapter in `engine/adapters.py` is the template: subclass `Adapter`,
implement `discover()` (find session files, derive project/session identity)
and `map_record()` (native record → canonical shape), register in `ADAPTERS`,
add tests mirroring `tests/test_adapters.py`. The hard part is usually
documenting the agent's on-disk format — start there.

## Ground rules

- **The engine stays stdlib-only** (`engine/*.py`, Python 3.9+). No pip
  dependencies in the engine; `pytest` is a dev-only convenience.
- **Privacy is a feature.** Transcripts are read locally and mined locally.
  Do not add network calls to the engine.
- Building a review UI? Follow [docs/REVIEW-CLIENTS.md](docs/REVIEW-CLIENTS.md).

## Workflow

1. Fork, branch, make the change.
2. Run the tests: `python3 -m pytest tests/` (all green before review; CI runs
   them on Python 3.9/3.12 across Linux and macOS).
3. For engine changes, also run `python3 engine/extractor.py --self-test`.
4. Open a PR with a clear description of the behavior change.

## Developer Certificate of Origin

Commits must be signed off (`git commit -s`), certifying the
[DCO](https://developercertificate.org/): you wrote the change or otherwise
have the right to submit it under the Apache 2.0 license.
