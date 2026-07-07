## What kind of change is this?

- [ ] **Adapter / exporter** (new trace source) — welcome! See CONTRIBUTING.md.
- [ ] **Risk-lint or redaction pattern** — welcome! Include a test with a realistic sample.
- [ ] **Bug fix**
- [ ] **Feature** — heads-up: features face a high bar here. The engine's core
      promise is a codebase small enough to read in an afternoon, so we prefer
      extension points over additions. Consider opening an issue first.

## Checklist

- [ ] `python3 -m pytest tests/` passes
- [ ] Engine stays **stdlib-only** (no new imports outside the standard library)
- [ ] No network calls added to the engine
- [ ] Commits are signed off (`git commit -s`, DCO)

## What does this change do?
