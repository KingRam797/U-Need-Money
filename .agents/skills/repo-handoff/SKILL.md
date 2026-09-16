---
name: repo-handoff
description: Preserve exact repository state when substantial work pauses, ends, or risks interruption.
---

# Repository Handoff

Update `.agent/PROGRESS.md` with only:
- completed work;
- files materially changed;
- verification performed and its result;
- unresolved errors or blockers;
- the exact next executable step.

Prefer one current checkpoint over a running diary. Preserve still-relevant prior blockers and decisions. Do not claim tests, commits, pushes, or deployments that were not verified.
