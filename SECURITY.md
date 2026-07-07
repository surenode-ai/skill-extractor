# Security policy

## Reporting a vulnerability

Please do not open public issues for security problems.

Report privately via GitHub's private vulnerability reporting on this
repository (Security tab → "Report a vulnerability"), or email
security@surenode.ai. We aim to acknowledge reports within 3 business days.

## The security model in one paragraph

This tool mines **private agent traces**, may send excerpts to a **remote LLM
backend**, and turns approved model output into **persistent agent
instructions**. That loop is the product and also the attack surface. The
shipped defaults are privacy-first: secret redaction before any prompt leaves
the process, `0600` files in `0700` state dirs, no `shell=True` anywhere,
explicit opt-in for non-default mining backends, and a risk lint plus
explicit-acknowledgement gate before any skill is installed.

## Scope notes

- The engine reads coding-agent transcripts locally and calls the LLM backend
  you configure. It makes no other network calls; anything that changes that
  is a vulnerability.
- Anything that causes transcript text to bypass redaction on its way to a
  mining backend is a vulnerability, as is anything that weakens state-file
  permissions.
- Mined skills are written locally after explicit human review. Bugs that
  bypass the review step or the risk-acknowledgement gate (installing a
  flagged candidate without a recorded decision) are in scope and treated as
  high severity.
