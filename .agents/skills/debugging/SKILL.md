---
name: debugging
description: Diagnose a reproducible repository defect with targeted inspection and proportionate verification.
---

# Debugging

1. Restate the observed failure and expected behavior.
2. Reproduce it with the narrowest reliable check.
3. Inspect the smallest likely file set; widen only from evidence.
4. Identify the cause before changing code.
5. Implement a fix only when the request authorizes changes.
6. Run the narrowest test that could disprove the fix, then broader checks only if justified.
7. Record unresolved failures and the exact next action in `.agent/PROGRESS.md`.

Keep relevant error text, file paths, and failed commands. Omit unrelated successful output.
