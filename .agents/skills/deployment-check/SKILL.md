---
name: deployment-check
description: Verify deployment readiness or diagnose a failed deployment without exposing credentials or changing production unexpectedly.
---

# Deployment Check

Determine the target platform and expected branch. Check only relevant build settings, runtime versions, environment-variable names, routing, framework configuration, and available build failures. Never expose secret values.

Run the production build locally when practical. Distinguish code failures from account, billing, permission, and provider incidents. Do not deploy, promote, roll back, or alter production settings unless authorized. Record the verified blocker and next step in `.agent/PROGRESS.md`.
