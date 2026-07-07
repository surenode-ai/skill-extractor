# Building a review client (hygiene checklist)

The bundled VS Code panel is one review client; INTEGRATION.md Pattern A
invites you to build your own (watch `pending.json`, act via `review.py`).
A review client handles transcript-derived text and installs persistent agent
instructions, so it inherits real security obligations. This is the checklist
the bundled panel follows; hold your client to the same bar.

## Webview / HTML surfaces

- **Content-Security-Policy, nonce-based.** `default-src 'none'`, allow only
  your own script block via a per-render nonce. No remote loads of any kind.

  ```html
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-RANDOM';">
  <script nonce="RANDOM"> ... </script>
  ```

- **No inline event handlers.** `onclick=` and friends are blocked by the CSP
  anyway; use event delegation:

  ```js
  document.getElementById("list").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-act]");
    if (btn) act(btn.dataset.act, btn.dataset.id);
  });
  ```

- **Escape every candidate field before rendering.** Candidate text is model
  output derived from transcripts; treat it as untrusted in your DOM.

## Files and processes

- **Temp files are private and unpredictable.** Edited skill bodies are
  transcript-derived: write them inside a `mkdtemp` directory with mode
  `0600`, and clean up in a `finally` so failure paths do not leak them.
- **No shell.** Invoke `review.py` / `extractor.py` with an argv array
  (`execFile`, `subprocess.run([...], shell=False)`), never string-built shell
  commands.
- **State stays private.** If your client writes any state of its own, match
  the engine: `0600` files in `0700` directories.

## Review semantics

- **Never bypass the risk gate.** Install through `review.py install` (or
  `lib.install_skill(cand, acknowledge_risk=...)`); both enforce the risk
  lint. On the `{"error": "risky"}` response, show the findings and require an
  explicit, modal acknowledgement; never auto-acknowledge.
- **Record decisions.** Installs and rejections (with the user's reason) go
  through the same commands so `decisions.jsonl` stays complete; the reasons
  are the miner's learning signal.
- **Show what will be installed.** Display the candidate's actual fields (and
  ideally the exact `SKILL.md`, see issue #1) rather than a paraphrase; the
  user is approving a persistent agent instruction.

## Reference implementations

- `vscode-extension/extension.js` in this repo (local install semantics).
- Downstream governed-upload clients follow the same checklist with different
  review verbs; the invariants above are the part that must not vary.
