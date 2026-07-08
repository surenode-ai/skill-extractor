---
name: clean-up-wrong-path-immediately
description: "When files are accidentally created in the wrong directory, remove them immediately before proceeding to the correct location."
---

# Immediately Clean Up Files Written to Wrong Location

**When to use:** When you realize you've written files to the wrong project directory or path.

## Rule

1. As soon as you realize a file was created in the wrong location, **immediately delete it** before doing anything else.
2. Use `rm -f <file>` and `rmdir <dir>` to clean up.
3. Confirm cleanup with an echo/message.
4. Only then proceed to the correct location.

**Why**: Stray files in wrong repos cause confusion, potential git commits of garbage, and clutter. The longer they exist, the more likely they'll be forgotten.

**Prevention**: Before writing files, confirm the correct project root by checking for existing project markers (package.json, README, .git, existing index.html).

---
*Mined by skill-extractor from a local repo (confidence 0.7, utility 0.629, trace outcome success). Review & edit as needed.*
