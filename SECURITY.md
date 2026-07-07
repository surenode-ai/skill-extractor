# Security policy

## Reporting a vulnerability

Please do not open public issues for security problems.

Report privately via GitHub's private vulnerability reporting on this
repository (Security tab → "Report a vulnerability"), or email
security@surenode.ai. We aim to acknowledge reports within 3 business days.

## Scope notes

- The engine reads coding-agent transcripts locally and calls the LLM backend
  you configure. It makes no other network calls; anything that changes that
  is a vulnerability.
- Mined skills are written locally after explicit human review. Bugs that
  bypass the review step (installing a candidate without a decision) are in
  scope and treated as high severity.
