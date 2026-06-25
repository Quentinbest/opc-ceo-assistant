# Status And Recovery

Run `opc-workspace status --format json` before a refresh, Apply, or Briefing when Workspace health is uncertain.

Branch on `schema_version` and `outcome`:

- `healthy`: continue the requested workflow.
- `degraded`: show diagnostic codes and bounded relative paths; do not display or open artifact contents in model context.
- `recovery_required`: stop new Apply operations and present the existing `pending_recovery` tokens for local recovery handling.

Use `imports`, `briefings`, `quarantine`, `cleanup`, and `audit` only as aggregate health. New run and revision references are SHA-256 fingerprints. Never infer business facts from counts, never read quarantined source records into model context, and never repair or delete corrupt artifacts through the Skill.

`pending_recovery` is the compatibility field used by local recovery operations. `recovery.runs` is the safe host-facing correlation list. Do not substitute one for the other.
