---
name: verify-baseline-before-changes
description: "Before implementing new features, run existing tests to confirm green baseline: this ensures any failures after changes are attributable to new code, not pre-existing issues."
---

# Always Verify Baseline Tests Before Making Changes

**When to use:** Before starting implementation of any new feature or phase in a project with existing tests.

1. Run the full test suite (`pytest -q` or equivalent) BEFORE making any code changes.
2. Confirm all tests pass and note the count (e.g., '3 passed').
3. If tests fail before your changes, investigate and fix or document before proceeding.
4. Also verify that required dependencies are available (try importing them), install if missing.
5. After implementation, run tests again to confirm no regressions AND that new tests pass.

---
*Mined by skill-extractor from a local repo (confidence 0.804, utility 0.713, trace outcome meh). Review & edit as needed.*
