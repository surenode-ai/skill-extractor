# Contributing

Thanks for helping improve skill-extractor.

## Ground rules

- **The engine stays stdlib-only** (`engine/*.py`, Python 3.9+). No pip
  dependencies in the engine; `pytest` is a dev-only convenience.
- **New trace sources are adapters.** Subclass `Adapter` in
  `engine/adapters.py` (implement `discover()` and `map_record()`), register it
  in `ADAPTERS`, and add tests. The Codex adapter is the template. If your
  agent's history is easy to export, a small exporter to the canonical record
  shape (docs/INTEGRATION.md §3) plus the existing `jsonl_dir` adapter may be
  all you need.
- **Privacy is a feature.** Transcripts are read locally and mined locally.
  Do not add network calls to the engine.

## Workflow

1. Fork, branch, make the change.
2. Run the tests: `python3 -m pytest tests/` (all green before review).
3. For engine changes, also run `python3 engine/extractor.py --self-test`.
4. Open a PR with a clear description of the behavior change.

## Developer Certificate of Origin

Commits must be signed off (`git commit -s`), certifying the
[DCO](https://developercertificate.org/): you wrote the change or otherwise
have the right to submit it under the Apache 2.0 license.
