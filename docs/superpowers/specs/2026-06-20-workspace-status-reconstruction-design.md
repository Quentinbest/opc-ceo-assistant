# Workspace Status Reconstruction Design

## Purpose

Complete Stage 1 Acceptance Criterion 14 by making `opc-workspace status` reconstruct
source, import, Brief, quarantine, recovery, cleanup, and audit health from the existing
Workspace artifacts.

The implementation must remain local-first, deterministic, privacy-bounded, and backward
compatible. It must not add a database, mutable status cache, background service, or repair
operation.

## Scope

### In scope

- Derive operational summaries from existing manifests, run directories, projections,
  canonical Briefs, decisions, and `logs/events.jsonl`.
- Return aggregate status sections for imports, Briefings, quarantine, recovery, cleanup,
  and audit health.
- Convert malformed or contradictory artifacts into typed diagnostics instead of allowing
  `status` to crash.
- Preserve the existing top-level `source`, `diagnostics`, `pending_recovery`, `cleanup`, and
  `outcome` fields.
- Maintain 100% Python statement and branch coverage.

### Out of scope

- Mutating or repairing import, Briefing, projection, or audit state.
- Adding a persistent health index, cache, database, or migration.
- Returning record IDs, titles, free text, disposition text, or raw source values.
- Changing import or Briefing transaction semantics.
- Implementing external-model fresh-context evaluation.

## Selected Approach

Reconstruct status on demand from authoritative and immutable artifacts.

This is preferred over a persistent health index because Stage 1 limits the source to 1,000
records and already treats the Workspace filesystem as the system of record. A derived index
would create another mutable state boundary and require its own locking, recovery, drift, and
migration contracts.

An event-log-only implementation is insufficient because audit events cannot prove that
expected canonical files, seals, projections, and current pointers still exist.

## Output Contract

`workspace_status()` retains its existing fields and adds the following sections.

### `imports`

```json
{
  "runs": 0,
  "states": {
    "staged_clean": 0,
    "staged_degraded": 0,
    "sealed": 0,
    "applied": 0,
    "blocked_or_corrupt": 0
  },
  "latest": {
    "run_id_hash": null,
    "drive_version": null,
    "observed_at": null,
    "state": null
  }
}
```

Import state is reconstructed per `inbox/imports/<run>/`:

- `applied` when a valid `apply_result.json` reports `outcome == "applied"`.
- `sealed` when a valid `seal.json` exists without an applied result.
- `staged_degraded` when a valid envelope reports `degraded == true` without a seal.
- `staged_clean` when a valid envelope reports `degraded == false` without a seal.
- `blocked_or_corrupt` when the run directory contains artifacts but no valid state can be
  reconstructed, or when required JSON has an invalid shape.

The latest run is selected by `envelope.exported_at`, then by directory name as a stable
tie-breaker. The host-facing result contains a SHA-256 run fingerprint, never the raw run ID.

### `briefings`

```json
{
  "runs": 0,
  "states": {
    "drafted": 0,
    "sealed": 0,
    "applied": 0,
    "blocked_or_corrupt": 0
  },
  "latest": {
    "run_id_hash": null,
    "brief_date": null,
    "revision_id_hash": null,
    "state": null
  },
  "canonical_revisions": 0,
  "decisions": 0
}
```

Briefing state is reconstructed per `inbox/briefings/<run>/`:

- `applied` when a valid `apply_result.json` reports `outcome == "applied"`.
- `sealed` when `seal.json`, `brief.json`, and `dispositions.json` are valid and no applied
  result exists.
- `drafted` when a valid `candidate_set.json` exists without a seal.
- `blocked_or_corrupt` otherwise.

Canonical revision and decision counts come from valid JSON files in `data/briefs/` and
`data/decisions/`. Current-pointer files are excluded from immutable revision counts.

### `quarantine`

```json
{
  "pending": 0,
  "rejected": 0,
  "by_domain": {
    "priority": 0,
    "pipeline": 0,
    "receivable": 0,
    "contract": 0,
    "project": 0,
    "risk": 0
  },
  "corrupt_projections": 0
}
```

Quarantine health is reconstructed from `data/source_projection/*.json`:

- Count `pending.status == "quarantined"` as `pending`.
- Count `pending.status == "rejected"` as `rejected`.
- Derive the domain from a valid `record_id` prefix.
- Count malformed projections separately and emit a diagnostic.
- Never return a record ID, source record, title, amount, or reason text.

### `recovery`

```json
{
  "required": 0,
  "runs": []
}
```

Recovery remains based on `apply_started - apply_completed` audit pairs. For backward
compatibility, the existing `pending_recovery` list remains present. The new `runs` list uses
`<kind>:sha256:<fingerprint>` rather than a raw run ID. The legacy `pending_recovery` values
remain unchanged as opaque local recovery tokens; this is the only raw-run-token compatibility
exception.

### `cleanup`

```json
{
  "retained_raw_copies": 0,
  "eligible_raw_copies": 0,
  "deleted_raw_copies": 0,
  "cleanup_errors": 0
}
```

The existing first-invocation deletion behavior remains unchanged. Status reports the counts
observed during that invocation. It does not expose raw-copy paths in the structured summary;
typed diagnostics may retain Workspace-relative paths for local repair.

### `audit`

```json
{
  "valid_events": 0,
  "malformed_events": 0,
  "apply_started": 0,
  "apply_completed": 0,
  "duplicate_completions": 0,
  "unknown_events": 0
}
```

Each nonblank JSONL line is parsed independently. A malformed line cannot hide valid events
before or after it. Required string fields are `event`, `kind`, and `run_id`. Recognized events
are `apply_started` and `apply_completed`; all other event names are counted as unknown.

## Diagnostics

The implementation adds these typed diagnostics:

- `IMPORT_STATUS_ARTIFACT_ERROR`: malformed or contradictory import run artifacts.
- `BRIEF_STATUS_ARTIFACT_ERROR`: malformed or contradictory Briefing run artifacts.
- `PROJECTION_STATUS_ARTIFACT_ERROR`: malformed source projection.
- `AUDIT_LOG_LINE_ERROR`: malformed JSONL line or invalid event shape.
- `AUDIT_DUPLICATE_COMPLETION`: more than one completion for the same kind/run pair.

Diagnostics contain only a code, Workspace-relative path or line number, and a bounded
machine-oriented reason. They must not include file contents, record values, or exception
tracebacks.

Any status artifact diagnostic makes the overall outcome `degraded`, unless unmatched apply
starts make the outcome `recovery_required`, which retains higher precedence.

## Architecture

Add private pure summarizers in `diagnostics.py` rather than a ninth production module:

- `_read_status_object(path)` validates a JSON object and returns a typed diagnostic on failure.
- `_summarize_imports(root)` reconstructs import run states.
- `_summarize_briefings(root)` reconstructs Briefing and canonical revision state.
- `_summarize_quarantine(root)` aggregates pending projection state.
- `_summarize_audit(root)` parses JSONL independently and derives recovery state.

`workspace_status()` remains the orchestrator. It runs cleanup first, validates the Workspace,
calls each summarizer, merges diagnostics, redacts source identity, and computes the final
outcome.

No summarizer writes files. The only permitted mutation remains the existing expired raw-copy
cleanup performed before validation.

## Data Flow

```text
Workspace status invocation
  -> expire eligible owned raw copies
  -> validate manifest, mirrors, permissions, and directories
  -> summarize import run directories
  -> summarize Briefing run directories and canonical artifacts
  -> summarize source projection quarantine state
  -> parse audit JSONL line by line
  -> derive unmatched recovery pairs
  -> redact source identifier
  -> merge typed diagnostics
  -> return healthy | degraded | recovery_required
```

## Privacy and Security

- Host-facing status never includes record IDs, titles, descriptions, amounts, free text,
  source rows, dispositions, or raw run IDs in newly added sections. The legacy
  `pending_recovery` field retains opaque run tokens for compatibility.
- Run and revision correlations use SHA-256 fingerprints.
- File references in diagnostics are Workspace-relative and resolved through the existing
  containment boundary.
- Status never follows raw-copy symlinks and does not broaden cleanup ownership rules.
- Corrupt artifacts are not repaired, deleted, or rewritten.

## Performance

The scan is bounded by Stage 1 limits and retained run history. It performs one sorted pass per
artifact class and no nested full-set scans. JSON files are read once per summarizer. The
existing status benchmark budget remains under 2 seconds and 100 MiB on the baseline machine.

If retained history later causes a reproducible budget failure, compaction or a derived index
requires a separate design and migration contract.

## Testing

Tests are written before implementation and cover:

- Empty initialized Workspace.
- Healthy clean import and applied Briefing history.
- Degraded import and quarantined/rejected projections.
- Sealed but unapplied import and Briefing runs.
- Interrupted Apply with unmatched audit start.
- Duplicate completion events.
- Malformed JSON, non-object JSON, malformed JSONL lines, and unknown audit events.
- Corrupt canonical Brief, decision, and projection artifacts.
- Latest-run ordering and stable tie-breaking.
- Privacy assertions proving raw IDs and business values are absent from serialized status.
- Existing cleanup, source redaction, version, permission, and recovery behavior.
- Updated status benchmark under the existing budget.

The final gate remains:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests evals spikes
uv run python -m opc_ceo.contracts generate --check
uv run pytest --cov=opc_ceo --cov-branch --cov-fail-under=100
uv run python -m opc_ceo.benchmark --phase status --records 1000 --warmup 3 --repeat 20 --stat p95
```

## Acceptance

The improvement is complete when:

1. Every required status section is present for empty and populated Workspaces.
2. Healthy, degraded, corrupt, cleanup, and recovery states are machine-distinguishable.
3. Malformed artifacts never crash `status` and always produce typed diagnostics.
4. Serialized status contains no raw record, revision, or spreadsheet identifiers; newly added
   sections contain no raw run IDs, and only legacy `pending_recovery` may retain opaque run
   tokens.
5. Existing callers remain compatible with the prior top-level fields.
6. Statement and branch coverage remain 100%.
7. The status benchmark remains within 2 seconds and 100 MiB p95.
