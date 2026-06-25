# Intake Reference

Normal `add` and `change` rows may use batch `approve` or `reject`. `conflict`, `tombstone_pending`, and `amount_anomaly` require an explicit per-record decision. `quarantined` rows cannot be approved.

Resolution format:

```json
{"batch":"approve","items":{"record_id":"reject"}}
```

The resolve seal binds `envelope.json`, `normalized.jsonl`, `diff.json`, `resolution.json`, `apply_plan.json`, and current projection preconditions. Never modify these after review.
