---
name: match-existing-design-system-before-editing
description: "Before adding new UI elements to an existing site, read the stylesheet to extract design tokens (colors, radii, font, button classes) so new elements look native."
---

# Read Existing Styles/Tokens Before Adding UI Components

**When to use:** When modifying an existing website or UI to add new components (modals, forms, sections).

## Procedure

1. **Read the existing CSS** (or design tokens file) before writing any new markup.
2. **Extract key tokens**: colors (`--accent`, `--bg`, `--text`), border-radius, font family, spacing scale, existing component classes (`.btn`, `.btn-primary`, `.btn-ghost`).
3. **Reuse existing classes** on new elements rather than inventing new ones.
4. **Match interaction patterns**: if existing buttons use `<a class="btn ...">`, decide whether new interactive elements should be `<button>` (semantic) but keep the same class names.
5. **Verify visually** or by inspection that new elements inherit the same custom properties.

This avoids the common mistake of adding components that look foreign to the existing design.

---
*Mined by skill-extractor from a local repo (confidence 0.88, utility 0.652, trace outcome success). Review & edit as needed.*
