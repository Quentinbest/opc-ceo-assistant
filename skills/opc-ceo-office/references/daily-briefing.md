# Daily Briefing Reference

Disposition shapes are strict:

```json
{
  "T1": {"kind": "act", "next_action": "..."},
  "T2": {"kind": "defer", "review_date": "YYYY-MM-DD"},
  "T3": {"kind": "delegate", "owner_alias": "...", "review_date": "YYYY-MM-DD"}
}
```

`dismiss` requires `reason`. Unknown or missing tokens and fields block rendering.

When Connector refresh fails and source age is at most 168 elapsed hours, explain the age and require explicit stale approval before adding `--allow-stale`. Older source is diagnostic-only and must produce no Top or dispositions.
