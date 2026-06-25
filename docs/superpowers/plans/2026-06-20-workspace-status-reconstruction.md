# Workspace Status Reconstruction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `opc-workspace status` deterministically reconstruct import, Briefing, quarantine, recovery, cleanup, and audit health from existing Workspace artifacts without exposing sensitive business data.

**Architecture:** Keep `workspace_status()` as the orchestration boundary in `diagnostics.py` and add small private filesystem summarizers for each artifact class. Every summarizer is read-only, returns an aggregate plus typed diagnostics, and tolerates malformed files; the only mutation remains the existing expired raw-copy cleanup. Preserve the existing top-level fields and add new aggregate sections, with `recovery_required` taking precedence over `degraded` and `healthy`.

**Tech Stack:** Python 3.12, `pathlib`, standard-library `json`/`hashlib`/`datetime`/`collections`, pytest, pytest-cov, Ruff, mypy, uv.

---

## Source Of Truth

Implement against the approved design:

- `docs/superpowers/specs/2026-06-20-workspace-status-reconstruction-design.md`
- Stage 1 Acceptance Criterion 14 in `OPC_CEO_stage1_implementation_plan.md:620`

Do not add a database, cache, background process, migration, repair command, or new production module. Do not change import or Briefing transaction semantics.

## Repository Constraint

`git rev-parse --verify HEAD` currently fails because the repository has no initial commit, and all project files are untracked. The steps below therefore use local verification checkpoints, not commits. Do not initialize history, stage files, or commit unless the user explicitly authorizes it during execution.

## File Map

- Create `tests/unit/test_status_reconstruction.py`: focused unit contract for status reconstruction, corruption handling, deterministic ordering, and privacy.
- Modify `src/opc_ceo/diagnostics.py:15-117`: cleanup inventory, bounded JSON reader, artifact summarizers, audit parser, source redaction, and final outcome orchestration.
- Modify `tests/integration/test_import_workflow.py:90-120`: prove a real applied import is reconstructed as `applied`.
- Modify `tests/integration/test_briefing_workflow.py:70-113`: prove a real applied Briefing, canonical revision, and decisions are reconstructed.
- Modify `src/opc_ceo/benchmark.py:48-93`: seed representative retained status history before timing and measure only reconstruction.
- Modify `tests/unit/test_benchmark.py:58-76`: prove the status benchmark fixture contains the intended history and still runs in isolation.
- Create `skills/opc-ceo-office/references/status.md`: operator rules for interpreting health, diagnostics, and recovery without exposing artifacts.
- Modify `skills/opc-ceo-office/SKILL.md:29-44`: route status requests to the new reference.

## Stable Contracts

Use these exact diagnostic codes:

```python
IMPORT_STATUS_ARTIFACT_ERROR = "IMPORT_STATUS_ARTIFACT_ERROR"
BRIEF_STATUS_ARTIFACT_ERROR = "BRIEF_STATUS_ARTIFACT_ERROR"
PROJECTION_STATUS_ARTIFACT_ERROR = "PROJECTION_STATUS_ARTIFACT_ERROR"
AUDIT_LOG_LINE_ERROR = "AUDIT_LOG_LINE_ERROR"
AUDIT_DUPLICATE_COMPLETION = "AUDIT_DUPLICATE_COMPLETION"
```

Use only these bounded reasons in new diagnostics:

```python
{
    "missing_file",
    "unreadable",
    "invalid_json",
    "not_object",
    "invalid_shape",
    "contradictory_artifacts",
    "unexpected_pending_status",
    "duplicate_completion",
}
```

New diagnostics may contain `code`, Workspace-relative `path`, optional one-based `line`, and `reason`. They must not contain exception messages, raw IDs, file contents, titles, amounts, dispositions, or source values.

---

### Task 1: Establish The Empty-Workspace Contract And Safe JSON Reader

**Files:**
- Create: `tests/unit/test_status_reconstruction.py`
- Modify: `src/opc_ceo/diagnostics.py:1-14`
- Modify: `src/opc_ceo/diagnostics.py:80-117`

- [ ] **Step 1: Write the failing empty-contract and reader tests**

Create `tests/unit/test_status_reconstruction.py` with the shared fixture helpers and first tests:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opc_ceo.contracts import canonical_json_bytes
from opc_ceo.diagnostics import _read_status_object, workspace_status
from opc_ceo.workspace import initialize_workspace, secure_replace, secure_write


def initialized_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    assert initialize_workspace(root, approved=True) == "initialized"
    return root


def write_json(path: Path, value: Any) -> None:
    secure_replace(path, canonical_json_bytes(value))


def test_empty_workspace_has_complete_status_contract(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)

    result = workspace_status(root)

    assert result["imports"] == {
        "runs": 0,
        "states": {
            "staged_clean": 0,
            "staged_degraded": 0,
            "sealed": 0,
            "applied": 0,
            "blocked_or_corrupt": 0,
        },
        "latest": {
            "run_id_hash": None,
            "drive_version": None,
            "observed_at": None,
            "state": None,
        },
    }
    assert result["briefings"] == {
        "runs": 0,
        "states": {"drafted": 0, "sealed": 0, "applied": 0, "blocked_or_corrupt": 0},
        "latest": {
            "run_id_hash": None,
            "brief_date": None,
            "revision_id_hash": None,
            "state": None,
        },
        "canonical_revisions": 0,
        "decisions": 0,
    }
    assert result["quarantine"] == {
        "pending": 0,
        "rejected": 0,
        "by_domain": {
            "priority": 0,
            "pipeline": 0,
            "receivable": 0,
            "contract": 0,
            "project": 0,
            "risk": 0,
        },
        "corrupt_projections": 0,
    }
    assert result["recovery"] == {"required": 0, "runs": []}
    assert result["audit"] == {
        "valid_events": 0,
        "malformed_events": 0,
        "apply_started": 0,
        "apply_completed": 0,
        "duplicate_completions": 0,
        "unknown_events": 0,
    }
    assert result["outcome"] == "healthy"


def test_status_object_reader_bounds_parse_failures(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)
    invalid = root / "inbox" / "imports" / "run" / "envelope.json"
    invalid.parent.mkdir(mode=0o700)
    secure_write(invalid, b"{not-json", exclusive=True)

    value, diagnostic = _read_status_object(root, invalid, "IMPORT_STATUS_ARTIFACT_ERROR")

    assert value is None
    assert diagnostic == {
        "code": "IMPORT_STATUS_ARTIFACT_ERROR",
        "path": "inbox/imports/run/envelope.json",
        "reason": "invalid_json",
    }
    serialized = json.dumps(diagnostic)
    assert "not-json" not in serialized


def test_status_object_reader_rejects_non_objects_and_symlinks(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)
    non_object = root / "inbox" / "imports" / "array" / "envelope.json"
    non_object.parent.mkdir(mode=0o700)
    secure_write(non_object, b"[]", exclusive=True)
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret":"outside"}', encoding="utf-8")
    linked = root / "inbox" / "imports" / "linked" / "envelope.json"
    linked.parent.mkdir(mode=0o700)
    linked.symlink_to(outside)

    _, object_diagnostic = _read_status_object(
        root, non_object, "IMPORT_STATUS_ARTIFACT_ERROR"
    )
    _, link_diagnostic = _read_status_object(root, linked, "IMPORT_STATUS_ARTIFACT_ERROR")

    assert object_diagnostic is not None
    assert object_diagnostic["reason"] == "not_object"
    assert link_diagnostic is not None
    assert link_diagnostic["reason"] == "invalid_shape"
    assert "outside" not in json.dumps(link_diagnostic)
```

- [ ] **Step 2: Run the focused tests and confirm the contract is absent**

Run:

```bash
uv run pytest tests/unit/test_status_reconstruction.py -q
```

Expected: collection or assertion failure because `_read_status_object` and the new output sections do not exist.

- [ ] **Step 3: Add constants, aggregate constructors, hashing, and the safe reader**

Add `WorkspaceError` to the existing `opc_ceo.workspace` import, then add below the imports in
`src/opc_ceo/diagnostics.py`:

```python
STATUS_DOMAINS = ("priority", "pipeline", "receivable", "contract", "project", "risk")
STATUS_EVENT_KINDS = {"import", "briefing"}


def _fingerprint(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _status_path(root: Path, path: Path) -> str:
    return str(path.relative_to(root))


def _read_status_object(
    root: Path, path: Path, code: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    relative = path.relative_to(root)
    expected = root.expanduser().resolve() / relative
    try:
        resolved = resolve_workspace_path(root, relative)
        details = path.lstat()
    except WorkspaceError:
        return None, {"code": code, "path": str(relative), "reason": "invalid_shape"}
    except OSError:
        return None, {"code": code, "path": str(relative), "reason": "unreadable"}
    if resolved != expected or stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        return None, {"code": code, "path": str(relative), "reason": "invalid_shape"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None, {"code": code, "path": str(relative), "reason": "unreadable"}
    except (UnicodeError, json.JSONDecodeError):
        return None, {"code": code, "path": str(relative), "reason": "invalid_json"}
    if not isinstance(value, dict):
        return None, {"code": code, "path": str(relative), "reason": "not_object"}
    return value, None


def _empty_imports() -> dict[str, Any]:
    return {
        "runs": 0,
        "states": {
            "staged_clean": 0,
            "staged_degraded": 0,
            "sealed": 0,
            "applied": 0,
            "blocked_or_corrupt": 0,
        },
        "latest": {
            "run_id_hash": None,
            "drive_version": None,
            "observed_at": None,
            "state": None,
        },
    }


def _empty_briefings() -> dict[str, Any]:
    return {
        "runs": 0,
        "states": {"drafted": 0, "sealed": 0, "applied": 0, "blocked_or_corrupt": 0},
        "latest": {
            "run_id_hash": None,
            "brief_date": None,
            "revision_id_hash": None,
            "state": None,
        },
        "canonical_revisions": 0,
        "decisions": 0,
    }


def _empty_quarantine() -> dict[str, Any]:
    return {
        "pending": 0,
        "rejected": 0,
        "by_domain": dict.fromkeys(STATUS_DOMAINS, 0),
        "corrupt_projections": 0,
    }


def _empty_audit() -> dict[str, int]:
    return {
        "valid_events": 0,
        "malformed_events": 0,
        "apply_started": 0,
        "apply_completed": 0,
        "duplicate_completions": 0,
        "unknown_events": 0,
    }
```

Add the new empty sections to the dictionary returned by `workspace_status()`:

```python
        "imports": _empty_imports(),
        "briefings": _empty_briefings(),
        "quarantine": _empty_quarantine(),
        "recovery": {"required": len(recovery), "runs": []},
        "audit": _empty_audit(),
```

- [ ] **Step 4: Run the focused tests and static checks**

Run:

```bash
uv run pytest tests/unit/test_status_reconstruction.py -q
uv run ruff check src/opc_ceo/diagnostics.py tests/unit/test_status_reconstruction.py
uv run mypy src/opc_ceo/diagnostics.py
```

Expected: all commands pass.

- [ ] **Step 5: Record a local checkpoint without staging or committing**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; the new test and modified diagnostic module remain visible as local changes.

---

### Task 2: Report Raw-Copy Cleanup Inventory Without Changing Cleanup Semantics

**Files:**
- Modify: `tests/unit/test_status_reconstruction.py`
- Modify: `src/opc_ceo/diagnostics.py:15-41`
- Test: `tests/unit/test_boundaries.py:153-167`

- [ ] **Step 1: Add failing retained, eligible, deleted, and cleanup-error tests**

Update the import block with `os`, `UTC`, `datetime`, and `timedelta`, then append the helpers
and tests:

```python
import os
from datetime import UTC, datetime, timedelta


NOW = datetime(2026, 6, 20, 8, 0, tzinfo=UTC)


def raw_copy(root: Path, run_id: str, *, age: timedelta) -> Path:
    path = root / "inbox" / "imports" / run_id / "source.xlsx"
    path.parent.mkdir(mode=0o700)
    secure_write(path, b"xlsx", exclusive=True)
    timestamp = (NOW - age).timestamp()
    os.utime(path, (timestamp, timestamp))
    return path


def test_cleanup_reports_inventory_observed_during_status(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)
    retained = raw_copy(root, "retained", age=timedelta(hours=1))
    deleted = raw_copy(root, "deleted", age=timedelta(hours=169))

    result = workspace_status(root, now=NOW)

    assert result["cleanup"] == {
        "retained_raw_copies": 1,
        "eligible_raw_copies": 1,
        "deleted_raw_copies": 1,
        "cleanup_errors": 0,
    }
    assert retained.exists()
    assert not deleted.exists()


def test_cleanup_failure_is_counted_and_degrades_status(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)
    raw = root / "inbox" / "imports" / "unsafe" / "source.xlsx"
    raw.parent.mkdir(mode=0o700)
    raw.symlink_to(root / ".opc" / "manifest.json")
    timestamp = (NOW - timedelta(hours=169)).timestamp()
    os.utime(raw, (timestamp, timestamp), follow_symlinks=False)

    result = workspace_status(root, now=NOW)

    assert result["cleanup"]["eligible_raw_copies"] == 1
    assert result["cleanup"]["cleanup_errors"] == 1
    assert result["outcome"] == "degraded"
    assert any(item["code"] == "RAW_RETENTION_CLEANUP_ERROR" for item in result["diagnostics"])
```

Change the production signature only by adding a keyword-only clock to `workspace_status`; existing callers remain compatible:

```python
def workspace_status(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
```

- [ ] **Step 2: Run the cleanup tests and confirm failure**

Run:

```bash
uv run pytest tests/unit/test_status_reconstruction.py -k cleanup -q
```

Expected: failure because cleanup currently returns only `deleted_raw_copies` and `workspace_status()` has no injected clock.

- [ ] **Step 3: Implement one-pass cleanup inventory**

Replace the existing cleanup function with these two functions:

```python
def _cleanup_raw_copies(
    root: Path, *, now: datetime | None = None
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    summary = {
        "retained_raw_copies": 0,
        "eligible_raw_copies": 0,
        "deleted_raw_copies": 0,
        "cleanup_errors": 0,
    }
    manifest_path = root / ".opc" / "manifest.json"
    if not manifest_path.is_file():
        return summary, []
    manifest = load_manifest(root)
    retention = int(manifest["limits"]["raw_failure_retention_hours"]) * 3600
    current = (now or datetime.now(UTC)).timestamp()
    diagnostics: list[dict[str, Any]] = []
    for raw in sorted((root / "inbox" / "imports").glob("*/source.xlsx")):
        try:
            details = raw.lstat()
        except OSError:
            diagnostics.append(
                {
                    "code": "RAW_RETENTION_CLEANUP_ERROR",
                    "path": _status_path(root, raw),
                    "reason": "unreadable",
                }
            )
            continue
        if current - details.st_mtime <= retention:
            summary["retained_raw_copies"] += 1
            continue
        summary["eligible_raw_copies"] += 1
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
        ):
            diagnostics.append(
                {
                    "code": "RAW_RETENTION_CLEANUP_ERROR",
                    "path": _status_path(root, raw),
                    "reason": "invalid_shape",
                }
            )
            continue
        try:
            secure_unlink(raw)
        except OSError:
            diagnostics.append(
                {
                    "code": "RAW_RETENTION_CLEANUP_ERROR",
                    "path": _status_path(root, raw),
                    "reason": "unreadable",
                }
            )
            continue
        summary["deleted_raw_copies"] += 1
    summary["cleanup_errors"] = len(diagnostics)
    return summary, diagnostics


def cleanup_expired_raw_copies(
    root: Path, *, now: datetime | None = None
) -> tuple[int, list[dict[str, Any]]]:
    summary, diagnostics = _cleanup_raw_copies(root, now=now)
    return summary["deleted_raw_copies"], diagnostics
```

At the start of `workspace_status()` use the structured helper once:

```python
    cleanup, cleanup_diagnostics = _cleanup_raw_copies(root, now=now)
```

Return `"cleanup": cleanup` and remove the old single-count construction.

- [ ] **Step 4: Verify cleanup compatibility and focused coverage**

Run:

```bash
uv run pytest tests/unit/test_status_reconstruction.py -k cleanup -q
uv run pytest tests/unit/test_boundaries.py -k "cleanup or raw_copy" -q
```

Expected: all selected tests pass; `cleanup_expired_raw_copies()` still returns `(deleted_count, diagnostics)`.

- [ ] **Step 5: Record a local checkpoint**

Run:

```bash
git diff --check
uv run ruff check src/opc_ceo/diagnostics.py tests/unit/test_status_reconstruction.py
```

Expected: both commands pass.

---

### Task 3: Reconstruct Import Run State And Latest Metadata

**Files:**
- Modify: `tests/unit/test_status_reconstruction.py`
- Modify: `src/opc_ceo/diagnostics.py:80-117`

- [ ] **Step 1: Add failing import-state, ordering, and corruption tests**

Append these helpers and tests:

```python
def import_envelope(
    root: Path,
    run_id: str,
    *,
    exported_at: str,
    degraded: bool = False,
    drive_version: str = "3",
) -> Path:
    run_root = root / "inbox" / "imports" / run_id
    write_json(
        run_root / "envelope.json",
        {
            "exported_at": exported_at,
            "degraded": degraded,
            "drive": {"version": drive_version},
        },
    )
    return run_root


def test_import_statuses_and_latest_tie_breaker(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)
    clean = import_envelope(root, "run-clean", exported_at="2026-06-20T00:00:00Z")
    degraded = import_envelope(
        root, "run-degraded", exported_at="2026-06-20T01:00:00Z", degraded=True
    )
    sealed = import_envelope(root, "run-sealed", exported_at="2026-06-20T02:00:00Z")
    write_json(sealed / "seal.json", {"seal_sha256": "seal"})
    applied = import_envelope(
        root, "run-z-applied", exported_at="2026-06-20T02:00:00Z", drive_version="9"
    )
    write_json(applied / "seal.json", {"seal_sha256": "seal"})
    write_json(applied / "apply_result.json", {"outcome": "applied"})

    result = workspace_status(root)

    assert result["imports"]["runs"] == 4
    assert result["imports"]["states"] == {
        "staged_clean": 1,
        "staged_degraded": 1,
        "sealed": 1,
        "applied": 1,
        "blocked_or_corrupt": 0,
    }
    assert result["imports"]["latest"] == {
        "run_id_hash": _fingerprint_for_test("run-z-applied"),
        "drive_version": "9",
        "observed_at": "2026-06-20T02:00:00Z",
        "state": "applied",
    }
    assert "run-z-applied" not in json.dumps(result["imports"])


def _fingerprint_for_test(value: str) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def test_import_corruption_is_bounded_and_does_not_hide_other_runs(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)
    import_envelope(root, "healthy", exported_at="2026-06-20T00:00:00Z")
    corrupt = root / "inbox" / "imports" / "secret-run"
    corrupt.mkdir(mode=0o700)
    secure_write(corrupt / "envelope.json", b"[]", exclusive=True)
    write_json(corrupt / "apply_result.json", {"outcome": "applied"})

    result = workspace_status(root)

    assert result["imports"]["runs"] == 2
    assert result["imports"]["states"]["blocked_or_corrupt"] == 1
    assert result["outcome"] == "degraded"
    serialized = json.dumps(result["imports"])
    assert "secret-run" not in serialized
    assert any(item["code"] == "IMPORT_STATUS_ARTIFACT_ERROR" for item in result["diagnostics"])
```

- [ ] **Step 2: Run import-focused tests and confirm failure**

Run:

```bash
uv run pytest tests/unit/test_status_reconstruction.py -k import -q
```

Expected: failures because import directories are not scanned.

- [ ] **Step 3: Implement strict import reconstruction**

Add this private summarizer:

```python
def _parse_status_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _summarize_imports(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    summary = _empty_imports()
    diagnostics: list[dict[str, Any]] = []
    latest_key: tuple[datetime, str] | None = None
    imports_root = root / "inbox" / "imports"
    if not imports_root.is_dir():
        return summary, diagnostics
    for run_root in sorted(path for path in imports_root.iterdir() if path.is_dir()):
        summary["runs"] += 1
        envelope_path = run_root / "envelope.json"
        if not envelope_path.is_file():
            diagnostics.append(
                {
                    "code": "IMPORT_STATUS_ARTIFACT_ERROR",
                    "path": _status_path(root, envelope_path),
                    "reason": "missing_file",
                }
            )
            summary["states"]["blocked_or_corrupt"] += 1
            continue
        envelope, diagnostic = _read_status_object(
            root, envelope_path, "IMPORT_STATUS_ARTIFACT_ERROR"
        )
        if diagnostic is not None:
            diagnostics.append(diagnostic)
            summary["states"]["blocked_or_corrupt"] += 1
            continue
        assert envelope is not None
        drive = envelope.get("drive")
        observed_at = envelope.get("exported_at")
        observed = _parse_status_datetime(observed_at)
        if (
            not isinstance(envelope.get("degraded"), bool)
            or not isinstance(drive, dict)
            or not isinstance(drive.get("version"), str)
            or observed is None
        ):
            diagnostics.append(
                {
                    "code": "IMPORT_STATUS_ARTIFACT_ERROR",
                    "path": _status_path(root, envelope_path),
                    "reason": "invalid_shape",
                }
            )
            summary["states"]["blocked_or_corrupt"] += 1
            continue

        seal_path = run_root / "seal.json"
        result_path = run_root / "apply_result.json"
        seal: dict[str, Any] | None = None
        result: dict[str, Any] | None = None
        artifact_invalid = False
        for path in (seal_path, result_path):
            if path.is_file():
                value, item = _read_status_object(root, path, "IMPORT_STATUS_ARTIFACT_ERROR")
                if item is not None:
                    diagnostics.append(item)
                    artifact_invalid = True
                elif path == seal_path:
                    seal = value
                else:
                    result = value
        seal_valid = seal is None or isinstance(seal.get("seal_sha256"), str)
        if artifact_invalid:
            state = "blocked_or_corrupt"
        elif not seal_valid:
            diagnostics.append(
                {
                    "code": "IMPORT_STATUS_ARTIFACT_ERROR",
                    "path": _status_path(root, seal_path),
                    "reason": "invalid_shape",
                }
            )
            state = "blocked_or_corrupt"
        elif result is not None and (seal is None or result.get("outcome") != "applied"):
            diagnostics.append(
                {
                    "code": "IMPORT_STATUS_ARTIFACT_ERROR",
                    "path": _status_path(root, result_path),
                    "reason": "contradictory_artifacts",
                }
            )
            state = "blocked_or_corrupt"
        elif result is not None:
            state = "applied"
        elif seal is not None:
            state = "sealed"
        else:
            state = "staged_degraded" if envelope["degraded"] else "staged_clean"
        summary["states"][state] += 1
        candidate_key = (observed, run_root.name)
        if latest_key is None or candidate_key > latest_key:
            latest_key = candidate_key
            summary["latest"] = {
                "run_id_hash": _fingerprint(run_root.name),
                "drive_version": drive["version"],
                "observed_at": observed_at,
                "state": state,
            }
    return summary, diagnostics
```

In `workspace_status()`, call the summarizer after validation and return its aggregate:

```python
    imports, import_diagnostics = _summarize_imports(root)
    diagnostics.extend(import_diagnostics)
```

Replace `"imports": _empty_imports()` with `"imports": imports`.

- [ ] **Step 4: Run import tests, then formatting and typing**

Run:

```bash
uv run pytest tests/unit/test_status_reconstruction.py -k import -q
uv run ruff format src/opc_ceo/diagnostics.py tests/unit/test_status_reconstruction.py
uv run ruff check src/opc_ceo/diagnostics.py tests/unit/test_status_reconstruction.py
uv run mypy src/opc_ceo/diagnostics.py
```

Expected: all commands pass.

- [ ] **Step 5: Record a local checkpoint**

Run `git diff --check`; expect no output.

---

### Task 4: Reconstruct Briefing Runs, Canonical Revisions, And Decisions

**Files:**
- Modify: `tests/unit/test_status_reconstruction.py`
- Modify: `src/opc_ceo/diagnostics.py:80-117`

- [ ] **Step 1: Add failing Briefing state and canonical-artifact tests**

Append:

```python
def briefing_candidate(root: Path, run_id: str, *, brief_date: str) -> Path:
    run_root = root / "inbox" / "briefings" / run_id
    write_json(run_root / "candidate_set.json", {"brief_date": brief_date})
    return run_root


def seal_briefing(run_root: Path) -> None:
    write_json(run_root / "brief.json", {"brief_date": "2026-06-20"})
    write_json(run_root / "dispositions.json", {})
    write_json(run_root / "seal.json", {"seal_sha256": "seal"})


def test_briefing_states_latest_revision_and_canonical_counts(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)
    briefing_candidate(root, "draft", brief_date="2026-06-19")
    sealed = briefing_candidate(root, "sealed", brief_date="2026-06-20")
    seal_briefing(sealed)
    applied = briefing_candidate(root, "z-applied", brief_date="2026-06-20")
    seal_briefing(applied)
    write_json(
        applied / "apply_result.json",
        {"outcome": "applied", "brief_revision_id": "brief_20260620_daily_r001"},
    )
    write_json(
        root / "data" / "briefs" / "brief_20260620_daily_r001.json",
        {"brief_revision_id": "brief_20260620_daily_r001"},
    )
    write_json(
        root / "data" / "briefs" / "brief_20260620_daily.current.json",
        {"brief_revision_id": "brief_20260620_daily_r001"},
    )
    write_json(
        root / "data" / "decisions" / "decision_2026-06-20_t1_r001.json",
        {"decision_id": "decision_2026-06-20_t1_r001"},
    )

    result = workspace_status(root)

    assert result["briefings"]["runs"] == 3
    assert result["briefings"]["states"] == {
        "drafted": 1,
        "sealed": 1,
        "applied": 1,
        "blocked_or_corrupt": 0,
    }
    assert result["briefings"]["canonical_revisions"] == 1
    assert result["briefings"]["decisions"] == 1
    assert result["briefings"]["latest"] == {
        "run_id_hash": _fingerprint_for_test("z-applied"),
        "brief_date": "2026-06-20",
        "revision_id_hash": _fingerprint_for_test("brief_20260620_daily_r001"),
        "state": "applied",
    }


def test_corrupt_briefing_and_canonical_files_degrade_without_crashing(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)
    run_root = briefing_candidate(root, "private-brief-run", brief_date="2026-06-20")
    secure_write(run_root / "seal.json", b"null", exclusive=True)
    secure_write(
        root / "data" / "briefs" / "brief_20260620_daily_r001.json",
        b"{broken",
        exclusive=True,
    )
    secure_write(
        root / "data" / "decisions" / "decision_2026-06-20_t1_r001.json",
        b"[]",
        exclusive=True,
    )

    result = workspace_status(root)

    assert result["briefings"]["states"]["blocked_or_corrupt"] == 1
    assert result["briefings"]["canonical_revisions"] == 0
    assert result["briefings"]["decisions"] == 0
    assert result["outcome"] == "degraded"
    assert "private-brief-run" not in json.dumps(result["briefings"])
```

- [ ] **Step 2: Run Briefing-focused tests and confirm failure**

Run:

```bash
uv run pytest tests/unit/test_status_reconstruction.py -k briefing -q
```

Expected: failures because Briefing artifacts are not scanned.

- [ ] **Step 3: Implement Briefing and canonical artifact reconstruction**

Replace the existing datetime import with `from datetime import UTC, date, datetime`, then add
the summarizer:

```python
from datetime import UTC, date, datetime


def _valid_status_date(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _count_status_objects(
    root: Path, paths: list[Path], code: str, required_key: str
) -> tuple[int, list[dict[str, Any]]]:
    count = 0
    diagnostics: list[dict[str, Any]] = []
    for path in paths:
        value, diagnostic = _read_status_object(root, path, code)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
        elif value is not None and isinstance(value.get(required_key), str):
            count += 1
        else:
            diagnostics.append(
                {
                    "code": code,
                    "path": _status_path(root, path),
                    "reason": "invalid_shape",
                }
            )
    return count, diagnostics


def _summarize_briefings(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    summary = _empty_briefings()
    diagnostics: list[dict[str, Any]] = []
    latest_key: tuple[str, str] | None = None
    briefings_root = root / "inbox" / "briefings"
    if briefings_root.is_dir():
        for run_root in sorted(path for path in briefings_root.iterdir() if path.is_dir()):
            summary["runs"] += 1
            candidate_path = run_root / "candidate_set.json"
            if not candidate_path.is_file():
                diagnostics.append(
                    {
                        "code": "BRIEF_STATUS_ARTIFACT_ERROR",
                        "path": _status_path(root, candidate_path),
                        "reason": "missing_file",
                    }
                )
                summary["states"]["blocked_or_corrupt"] += 1
                continue
            candidate, diagnostic = _read_status_object(
                root, candidate_path, "BRIEF_STATUS_ARTIFACT_ERROR"
            )
            if diagnostic is not None:
                diagnostics.append(diagnostic)
                summary["states"]["blocked_or_corrupt"] += 1
                continue
            assert candidate is not None
            brief_date = candidate.get("brief_date")
            if not _valid_status_date(brief_date):
                diagnostics.append(
                    {
                        "code": "BRIEF_STATUS_ARTIFACT_ERROR",
                        "path": _status_path(root, candidate_path),
                        "reason": "invalid_shape",
                    }
                )
                summary["states"]["blocked_or_corrupt"] += 1
                continue

            artifact_names = ("seal.json", "brief.json", "dispositions.json", "apply_result.json")
            artifacts: dict[str, dict[str, Any]] = {}
            artifact_invalid = False
            for name in artifact_names:
                path = run_root / name
                if not path.is_file():
                    continue
                value, item = _read_status_object(root, path, "BRIEF_STATUS_ARTIFACT_ERROR")
                if item is not None:
                    diagnostics.append(item)
                    artifact_invalid = True
                elif value is not None:
                    artifacts[name] = value
            sealed_names = {"seal.json", "brief.json", "dispositions.json"}
            has_sealed_set = sealed_names <= artifacts.keys()
            result = artifacts.get("apply_result.json")
            revision_id = result.get("brief_revision_id") if result is not None else None
            seal = artifacts.get("seal.json")
            brief = artifacts.get("brief.json")
            sealed_shape_valid = (
                not has_sealed_set
                or (
                    seal is not None
                    and isinstance(seal.get("seal_sha256"), str)
                    and brief is not None
                    and brief.get("brief_date") == brief_date
                )
            )
            if artifact_invalid:
                state = "blocked_or_corrupt"
            elif not sealed_shape_valid:
                diagnostics.append(
                    {
                        "code": "BRIEF_STATUS_ARTIFACT_ERROR",
                        "path": _status_path(root, run_root),
                        "reason": "invalid_shape",
                    }
                )
                state = "blocked_or_corrupt"
            elif result is not None and (
                not has_sealed_set
                or result.get("outcome") != "applied"
                or not isinstance(revision_id, str)
            ):
                diagnostics.append(
                    {
                        "code": "BRIEF_STATUS_ARTIFACT_ERROR",
                        "path": _status_path(root, run_root / "apply_result.json"),
                        "reason": "contradictory_artifacts",
                    }
                )
                state = "blocked_or_corrupt"
            elif result is not None:
                state = "applied"
            elif has_sealed_set:
                state = "sealed"
            elif artifacts:
                diagnostics.append(
                    {
                        "code": "BRIEF_STATUS_ARTIFACT_ERROR",
                        "path": _status_path(root, run_root),
                        "reason": "contradictory_artifacts",
                    }
                )
                state = "blocked_or_corrupt"
            else:
                state = "drafted"
            summary["states"][state] += 1
            candidate_key = (str(brief_date), run_root.name)
            if latest_key is None or candidate_key > latest_key:
                latest_key = candidate_key
                summary["latest"] = {
                    "run_id_hash": _fingerprint(run_root.name),
                    "brief_date": brief_date,
                    "revision_id_hash": (
                        _fingerprint(revision_id) if isinstance(revision_id, str) else None
                    ),
                    "state": state,
                }

    revisions, revision_diagnostics = _count_status_objects(
        root,
        sorted((root / "data" / "briefs").glob("brief_*_r*.json")),
        "BRIEF_STATUS_ARTIFACT_ERROR",
        "brief_revision_id",
    )
    decisions, decision_diagnostics = _count_status_objects(
        root,
        sorted((root / "data" / "decisions").glob("decision_*.json")),
        "BRIEF_STATUS_ARTIFACT_ERROR",
        "decision_id",
    )
    summary["canonical_revisions"] = revisions
    summary["decisions"] = decisions
    diagnostics.extend(revision_diagnostics)
    diagnostics.extend(decision_diagnostics)
    return summary, diagnostics
```

Call it from `workspace_status()` and use the resulting aggregate:

```python
    briefings, briefing_diagnostics = _summarize_briefings(root)
    diagnostics.extend(briefing_diagnostics)
```

Replace `"briefings": _empty_briefings()` with `"briefings": briefings`.

- [ ] **Step 4: Run Briefing tests and static checks**

Run:

```bash
uv run pytest tests/unit/test_status_reconstruction.py -k briefing -q
uv run ruff format src/opc_ceo/diagnostics.py tests/unit/test_status_reconstruction.py
uv run ruff check src/opc_ceo/diagnostics.py tests/unit/test_status_reconstruction.py
uv run mypy src/opc_ceo/diagnostics.py
```

Expected: all commands pass.

- [ ] **Step 5: Record a local checkpoint**

Run `git diff --check`; expect no output.

---

### Task 5: Aggregate Quarantined And Rejected Source Projections

**Files:**
- Modify: `tests/unit/test_status_reconstruction.py`
- Modify: `src/opc_ceo/diagnostics.py:80-117`

- [ ] **Step 1: Add failing projection aggregate and privacy tests**

Append:

```python
def projection(root: Path, record_id: str, pending: object) -> None:
    write_json(
        root / "data" / "source_projection" / f"{record_id}.json",
        {"record_id": record_id, "pending": pending},
    )


def test_quarantine_counts_status_and_domain_without_identifiers(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)
    projection(root, "priority_growth", {"status": "quarantined", "source_record": {"title": "X"}})
    projection(root, "receivable_acme", {"status": "rejected", "reason_codes": ["private"]})
    projection(root, "risk_clear", None)

    result = workspace_status(root)

    assert result["quarantine"] == {
        "pending": 1,
        "rejected": 1,
        "by_domain": {
            "priority": 1,
            "pipeline": 0,
            "receivable": 1,
            "contract": 0,
            "project": 0,
            "risk": 0,
        },
        "corrupt_projections": 0,
    }
    serialized = json.dumps(result)
    assert "priority_growth" not in serialized
    assert "receivable_acme" not in serialized
    assert "private" not in serialized


def test_corrupt_projection_is_counted_and_diagnostic_is_bounded(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)
    write_json(
        root / "data" / "source_projection" / "secret-customer.json",
        {"record_id": "unknown_secret", "pending": {"status": "external-change"}},
    )

    result = workspace_status(root)

    assert result["quarantine"]["corrupt_projections"] == 1
    assert result["outcome"] == "degraded"
    serialized = json.dumps(result)
    assert "unknown_secret" not in serialized
    assert "external-change" not in serialized
    assert any(
        item["code"] == "PROJECTION_STATUS_ARTIFACT_ERROR"
        and item["reason"] == "unexpected_pending_status"
        for item in result["diagnostics"]
    )
```

- [ ] **Step 2: Run quarantine tests and confirm failure**

Run:

```bash
uv run pytest tests/unit/test_status_reconstruction.py -k "quarantine or projection" -q
```

Expected: failures because projection files are not scanned.

- [ ] **Step 3: Implement projection aggregation with one corrupt count per file**

Add:

```python
def _projection_domain(record_id: object) -> str | None:
    if not isinstance(record_id, str):
        return None
    return next((domain for domain in STATUS_DOMAINS if record_id.startswith(f"{domain}_")), None)


def _summarize_quarantine(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    summary = _empty_quarantine()
    diagnostics: list[dict[str, Any]] = []
    projection_root = root / "data" / "source_projection"
    if not projection_root.is_dir():
        return summary, diagnostics
    for path in sorted(projection_root.glob("*.json")):
        value, diagnostic = _read_status_object(root, path, "PROJECTION_STATUS_ARTIFACT_ERROR")
        if diagnostic is not None:
            diagnostics.append(diagnostic)
            summary["corrupt_projections"] += 1
            continue
        assert value is not None
        domain = _projection_domain(value.get("record_id"))
        pending = value.get("pending")
        if domain is None or (pending is not None and not isinstance(pending, dict)):
            diagnostics.append(
                {
                    "code": "PROJECTION_STATUS_ARTIFACT_ERROR",
                    "path": _status_path(root, path),
                    "reason": "invalid_shape",
                }
            )
            summary["corrupt_projections"] += 1
            continue
        if pending is None:
            continue
        status = pending.get("status")
        if status not in {"quarantined", "rejected"}:
            diagnostics.append(
                {
                    "code": "PROJECTION_STATUS_ARTIFACT_ERROR",
                    "path": _status_path(root, path),
                    "reason": "unexpected_pending_status",
                }
            )
            summary["corrupt_projections"] += 1
            continue
        if status == "quarantined":
            summary["pending"] += 1
        else:
            summary["rejected"] += 1
        summary["by_domain"][domain] += 1
    return summary, diagnostics
```

Call it from `workspace_status()` and use the resulting aggregate:

```python
    quarantine, projection_diagnostics = _summarize_quarantine(root)
    diagnostics.extend(projection_diagnostics)
```

Replace `"quarantine": _empty_quarantine()` with `"quarantine": quarantine`.

- [ ] **Step 4: Run projection tests and static checks**

Run:

```bash
uv run pytest tests/unit/test_status_reconstruction.py -k "quarantine or projection" -q
uv run ruff format src/opc_ceo/diagnostics.py tests/unit/test_status_reconstruction.py
uv run ruff check src/opc_ceo/diagnostics.py tests/unit/test_status_reconstruction.py
uv run mypy src/opc_ceo/diagnostics.py
```

Expected: all commands pass.

- [ ] **Step 5: Record a local checkpoint**

Run `git diff --check`; expect no output.

---

### Task 6: Parse Audit JSONL Independently And Derive Recovery

**Files:**
- Modify: `tests/unit/test_status_reconstruction.py`
- Modify: `src/opc_ceo/diagnostics.py:80-117`
- Test: `tests/unit/test_boundaries.py:120-151`
- Test: `tests/unit/test_cli_dispatch.py:30-60`

- [ ] **Step 1: Add failing mixed-log, duplicate, and precedence tests**

Append:

```python
def write_events(root: Path, lines: list[bytes]) -> None:
    (root / "logs" / "events.jsonl").write_bytes(b"\n".join(lines) + b"\n")


def test_audit_parser_keeps_valid_lines_around_malformed_line(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)
    write_events(
        root,
        [
            canonical_json_bytes(
                {"event": "apply_started", "kind": "import", "run_id": "private-run"}
            ).rstrip(),
            b"{broken",
            canonical_json_bytes(
                {"event": "future_event", "kind": "briefing", "run_id": "future-run"}
            ).rstrip(),
        ],
    )

    result = workspace_status(root)

    assert result["audit"] == {
        "valid_events": 2,
        "malformed_events": 1,
        "apply_started": 1,
        "apply_completed": 0,
        "duplicate_completions": 0,
        "unknown_events": 1,
    }
    assert result["pending_recovery"] == ["import:private-run"]
    assert result["recovery"] == {
        "required": 1,
        "runs": [f"import:{_fingerprint_for_test('private-run')}"],
    }
    assert result["outcome"] == "recovery_required"
    audit_errors = [item for item in result["diagnostics"] if item["code"] == "AUDIT_LOG_LINE_ERROR"]
    assert audit_errors == [
        {
            "code": "AUDIT_LOG_LINE_ERROR",
            "path": "logs/events.jsonl",
            "line": 2,
            "reason": "invalid_json",
        }
    ]


def test_duplicate_completion_is_counted_without_creating_recovery(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)
    event = canonical_json_bytes(
        {"event": "apply_completed", "kind": "briefing", "run_id": "duplicate-run"}
    ).rstrip()
    write_events(root, [event, event])

    result = workspace_status(root)

    assert result["audit"]["valid_events"] == 2
    assert result["audit"]["apply_completed"] == 2
    assert result["audit"]["duplicate_completions"] == 1
    assert result["pending_recovery"] == []
    assert result["outcome"] == "degraded"
    duplicate = next(
        item for item in result["diagnostics"] if item["code"] == "AUDIT_DUPLICATE_COMPLETION"
    )
    assert duplicate["line"] == 2
    assert "duplicate-run" not in json.dumps(duplicate)


def test_audit_rejects_log_symlink_without_reading_target(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)
    outside = tmp_path / "outside-events.jsonl"
    outside.write_text(
        '{"event":"apply_started","kind":"import","run_id":"outside-secret"}\n',
        encoding="utf-8",
    )
    event_path = root / "logs" / "events.jsonl"
    event_path.unlink()
    event_path.symlink_to(outside)

    result = workspace_status(root)

    assert result["audit"]["valid_events"] == 0
    assert result["outcome"] == "degraded"
    assert "outside-secret" not in json.dumps(result)
    assert any(
        item["code"] == "AUDIT_LOG_LINE_ERROR" and item["reason"] == "invalid_shape"
        for item in result["diagnostics"]
    )
```

- [ ] **Step 2: Run audit and legacy recovery tests and confirm failure**

Run:

```bash
uv run pytest tests/unit/test_status_reconstruction.py -k "audit or completion or recovery" -q
uv run pytest tests/unit/test_boundaries.py -k recovery -q
uv run pytest tests/unit/test_cli_dispatch.py -k status_recovery -q
```

Expected: the new tests fail because malformed JSONL currently crashes status and audit counts are absent; legacy tests still show the compatibility contract.

- [ ] **Step 3: Implement line-isolated audit parsing**

Add `from collections import Counter` to the top-level import block, then add:

```python
def _audit_line_diagnostic(path: str, line: int, reason: str) -> dict[str, Any]:
    return {"code": "AUDIT_LOG_LINE_ERROR", "path": path, "line": line, "reason": reason}


def _summarize_audit(
    root: Path,
) -> tuple[dict[str, int], list[tuple[str, str]], list[dict[str, Any]]]:
    summary = _empty_audit()
    diagnostics: list[dict[str, Any]] = []
    started: set[tuple[str, str]] = set()
    completed: Counter[tuple[str, str]] = Counter()
    event_path = root / "logs" / "events.jsonl"
    relative = _status_path(root, event_path)
    if not event_path.exists() and not event_path.is_symlink():
        diagnostics.append(
            {"code": "AUDIT_LOG_LINE_ERROR", "path": relative, "reason": "missing_file"}
        )
        return summary, [], diagnostics
    try:
        details = event_path.lstat()
        resolved = resolve_workspace_path(root, event_path.relative_to(root))
    except WorkspaceError:
        diagnostics.append(
            {"code": "AUDIT_LOG_LINE_ERROR", "path": relative, "reason": "invalid_shape"}
        )
        return summary, [], diagnostics
    except OSError:
        diagnostics.append(
            {"code": "AUDIT_LOG_LINE_ERROR", "path": relative, "reason": "unreadable"}
        )
        return summary, [], diagnostics
    expected = root.expanduser().resolve() / event_path.relative_to(root)
    if resolved != expected or stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        diagnostics.append(
            {"code": "AUDIT_LOG_LINE_ERROR", "path": relative, "reason": "invalid_shape"}
        )
        return summary, [], diagnostics
    try:
        lines = event_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        diagnostics.append(
            {"code": "AUDIT_LOG_LINE_ERROR", "path": relative, "reason": "unreadable"}
        )
        return summary, [], diagnostics
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            summary["malformed_events"] += 1
            diagnostics.append(_audit_line_diagnostic(relative, line_number, "invalid_json"))
            continue
        if (
            not isinstance(event, dict)
            or not isinstance(event.get("event"), str)
            or event.get("kind") not in STATUS_EVENT_KINDS
            or not isinstance(event.get("run_id"), str)
        ):
            summary["malformed_events"] += 1
            diagnostics.append(_audit_line_diagnostic(relative, line_number, "invalid_shape"))
            continue
        summary["valid_events"] += 1
        event_name = event["event"]
        pair = (event["kind"], event["run_id"])
        if event_name == "apply_started":
            summary["apply_started"] += 1
            started.add(pair)
        elif event_name == "apply_completed":
            summary["apply_completed"] += 1
            completed[pair] += 1
            if completed[pair] > 1:
                summary["duplicate_completions"] += 1
                diagnostics.append(
                    {
                        "code": "AUDIT_DUPLICATE_COMPLETION",
                        "path": relative,
                        "line": line_number,
                        "reason": "duplicate_completion",
                    }
                )
        else:
            summary["unknown_events"] += 1
    recovery = sorted(started - set(completed))
    return summary, recovery, diagnostics
```

Remove the existing all-at-once event parsing from `workspace_status()` and replace it with:

```python
    audit, recovery_pairs, audit_diagnostics = _summarize_audit(root)
    diagnostics.extend(audit_diagnostics)
    pending_recovery = [f"{kind}:{run_id}" for kind, run_id in recovery_pairs]
    recovery = {
        "required": len(recovery_pairs),
        "runs": [f"{kind}:{_fingerprint(run_id)}" for kind, run_id in recovery_pairs],
    }
    if recovery_pairs:
        diagnostics.append(
            {
                "code": "PARTIAL_APPLY_RECOVERY_REQUIRED",
                "path": pending_recovery[0],
            }
        )
```

Return `pending_recovery`, `recovery`, and `audit` from these values. Preserve the legacy raw recovery token only in `pending_recovery` and the existing recovery diagnostic path.

- [ ] **Step 4: Make diagnostic order deterministic and apply outcome precedence**

Add:

```python
def _diagnostic_key(item: dict[str, Any]) -> tuple[str, str, int, str]:
    return (
        str(item.get("code", "")),
        str(item.get("path", "")),
        int(item.get("line", 0)),
        str(item.get("reason", "")),
    )
```

Immediately before returning from `workspace_status()`:

```python
    diagnostics.sort(key=_diagnostic_key)
    outcome = (
        "recovery_required"
        if recovery_pairs
        else ("degraded" if diagnostics else "healthy")
    )
```

Return `"outcome": outcome`.

- [ ] **Step 5: Run audit, compatibility, formatting, and typing checks**

Run:

```bash
uv run pytest tests/unit/test_status_reconstruction.py -k "audit or completion or recovery" -q
uv run pytest tests/unit/test_boundaries.py -k recovery -q
uv run pytest tests/unit/test_cli_dispatch.py -k status_recovery -q
uv run ruff format src/opc_ceo/diagnostics.py tests/unit/test_status_reconstruction.py
uv run ruff check src/opc_ceo/diagnostics.py tests/unit/test_status_reconstruction.py
uv run mypy src/opc_ceo/diagnostics.py
```

Expected: all commands pass.

- [ ] **Step 6: Record a local checkpoint**

Run `git diff --check`; expect no output.

---

### Task 7: Prove Real Workflows, Backward Compatibility, Privacy, And Operator Guidance

**Files:**
- Modify: `tests/integration/test_import_workflow.py:90-120`
- Modify: `tests/integration/test_briefing_workflow.py:70-113`
- Modify: `tests/unit/test_status_reconstruction.py`
- Create: `skills/opc-ceo-office/references/status.md`
- Modify: `skills/opc-ceo-office/SKILL.md:29-44`

- [ ] **Step 1: Add assertions to the real import workflow**

Import `workspace_status` in `tests/integration/test_import_workflow.py`:

```python
from opc_ceo.diagnostics import workspace_status
```

After the idempotent apply assertion in `test_stage_resolve_apply_is_sealed_and_idempotent`, add:

```python
    status = workspace_status(root)
    assert status["imports"]["runs"] == 1
    assert status["imports"]["states"]["applied"] == 1
    assert status["imports"]["latest"]["drive_version"] == "3"
    assert status["imports"]["latest"]["state"] == "applied"
```

- [ ] **Step 2: Add assertions to the real Briefing workflow**

Import `workspace_status` in `tests/integration/test_briefing_workflow.py`:

```python
from opc_ceo.diagnostics import workspace_status
```

After the idempotent apply assertion in `test_briefing_requires_dispositions_and_sealed_apply`, add:

```python
    status = workspace_status(root)
    assert status["briefings"]["runs"] == 1
    assert status["briefings"]["states"]["applied"] == 1
    assert status["briefings"]["canonical_revisions"] == 1
    assert status["briefings"]["decisions"] == 2
    assert status["briefings"]["latest"]["state"] == "applied"
```

- [ ] **Step 3: Add a whole-result privacy regression test**

Append to `tests/unit/test_status_reconstruction.py`:

```python
def test_new_status_sections_never_expose_business_values_or_raw_ids(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)
    run_root = import_envelope(
        root, "import-private-raw-id", exported_at="2026-06-20T00:00:00Z"
    )
    write_json(run_root / "seal.json", {"seal_sha256": "private-seal"})
    projection(
        root,
        "pipeline_private_customer",
        {
            "status": "quarantined",
            "source_record": {"title": "Private Customer", "amount": "999999.00"},
        },
    )
    brief_root = briefing_candidate(root, "brief-private-raw-id", brief_date="2026-06-20")
    seal_briefing(brief_root)

    result = workspace_status(root)
    new_sections = {
        key: result[key]
        for key in ("imports", "briefings", "quarantine", "recovery", "cleanup", "audit")
    }
    serialized = json.dumps(new_sections, sort_keys=True)

    for forbidden in (
        "import-private-raw-id",
        "brief-private-raw-id",
        "pipeline_private_customer",
        "Private Customer",
        "999999.00",
        "private-seal",
    ):
        assert forbidden not in serialized
```

- [ ] **Step 4: Run workflow and privacy tests**

Run:

```bash
uv run pytest tests/integration/test_import_workflow.py::test_stage_resolve_apply_is_sealed_and_idempotent -q
uv run pytest tests/integration/test_briefing_workflow.py::test_briefing_requires_dispositions_and_sealed_apply -q
uv run pytest tests/unit/test_status_reconstruction.py -k privacy -q
```

Expected: all tests pass. If a real artifact has a stricter shape than a synthetic fixture, tighten the focused fixture to match production rather than loosening the summarizer.

- [ ] **Step 5: Add operator status guidance**

Create `skills/opc-ceo-office/references/status.md`:

```markdown
# Status And Recovery

Run `opc-workspace status --format json` before a refresh, Apply, or Briefing when Workspace health is uncertain.

Branch on `schema_version` and `outcome`:

- `healthy`: continue the requested workflow.
- `degraded`: show diagnostic codes and bounded relative paths; do not display or open artifact contents in model context.
- `recovery_required`: stop new Apply operations and present the existing `pending_recovery` tokens for local recovery handling.

Use `imports`, `briefings`, `quarantine`, `cleanup`, and `audit` only as aggregate health. New run and revision references are SHA-256 fingerprints. Never infer business facts from counts, never read quarantined source records into model context, and never repair or delete corrupt artifacts through the Skill.

`pending_recovery` is the compatibility field used by local recovery operations. `recovery.runs` is the safe host-facing correlation list. Do not substitute one for the other.
```

Add this section before `## Boundaries` in `skills/opc-ceo-office/SKILL.md`:

```markdown
## Status And Recovery

Run `opc-workspace status --format json` when checking health or before resuming an interrupted Apply. Branch on `outcome`; stop on `recovery_required`, and never expose artifact contents while explaining `degraded` diagnostics.

Read [status.md](references/status.md) for aggregate interpretation and recovery-token rules.
```

- [ ] **Step 6: Verify all status tests and installer packaging**

Run:

```bash
uv run pytest tests/unit/test_status_reconstruction.py tests/integration/test_import_workflow.py tests/integration/test_briefing_workflow.py tests/integration/test_cli_installer.py -q
uv run ruff check tests/unit/test_status_reconstruction.py tests/integration/test_import_workflow.py tests/integration/test_briefing_workflow.py
```

Expected: all commands pass; installer tests prove the new reference remains part of the skill directory copied by the installer.

- [ ] **Step 7: Record a local checkpoint**

Run `git diff --check`; expect no output.

---

### Task 8: Benchmark Representative Retained Status History

**Files:**
- Modify: `src/opc_ceo/benchmark.py:48-93`
- Modify: `tests/unit/test_benchmark.py:58-76`

- [ ] **Step 1: Add a failing benchmark-fixture test**

Add these imports to `tests/unit/test_benchmark.py`:

```python
from opc_ceo.diagnostics import workspace_status
```

Add:

```python
def test_status_benchmark_fixture_scales_records_and_bounds_run_history(tmp_path: Path) -> None:
    root = tmp_path / "workspace"

    benchmark._prepare_status_workspace(root, records=25)
    status = workspace_status(root)

    assert len(list((root / "data" / "source_projection").glob("*.json"))) == 25
    assert status["imports"]["runs"] == 20
    assert status["briefings"]["runs"] == 20
    assert status["audit"]["valid_events"] == 80
    assert status["outcome"] == "healthy"
```

- [ ] **Step 2: Run the benchmark fixture test and confirm failure**

Run:

```bash
uv run pytest tests/unit/test_benchmark.py -k status_benchmark_fixture -q
```

Expected: failure because `_prepare_status_workspace` does not exist.

- [ ] **Step 3: Seed valid projections, import runs, Briefing runs, and audit history**

Add `secure_append` and `secure_mkdir` to the workspace imports in `src/opc_ceo/benchmark.py`, then add:

```python
def _prepare_status_workspace(root: Path, *, records: int) -> None:
    initialize_workspace(root, approved=True)
    for index in range(records):
        record_id = f"priority_{index:04d}"
        projection = {"record_id": record_id, "pending": None}
        secure_replace(
            root / "data" / "source_projection" / f"{record_id}.json",
            canonical_json_bytes(projection),
        )

    run_count = min(records, 20)
    for index in range(run_count):
        import_id = f"benchmark-import-{index:04d}"
        import_root = root / "inbox" / "imports" / import_id
        secure_mkdir(import_root)
        secure_replace(
            import_root / "envelope.json",
            canonical_json_bytes(
                {
                    "exported_at": f"2026-06-20T00:{index:02d}:00Z",
                    "degraded": False,
                    "drive": {"version": str(index + 1)},
                }
            ),
        )
        secure_replace(import_root / "seal.json", canonical_json_bytes({"seal_sha256": "seal"}))
        secure_replace(
            import_root / "apply_result.json",
            canonical_json_bytes({"outcome": "applied"}),
        )

        brief_id = f"benchmark-brief-{index:04d}"
        brief_root = root / "inbox" / "briefings" / brief_id
        secure_mkdir(brief_root)
        secure_replace(
            brief_root / "candidate_set.json",
            canonical_json_bytes({"brief_date": "2026-06-20"}),
        )
        secure_replace(
            brief_root / "brief.json", canonical_json_bytes({"brief_date": "2026-06-20"})
        )
        secure_replace(brief_root / "dispositions.json", canonical_json_bytes({}))
        secure_replace(brief_root / "seal.json", canonical_json_bytes({"seal_sha256": "seal"}))
        secure_replace(
            brief_root / "apply_result.json",
            canonical_json_bytes(
                {
                    "outcome": "applied",
                    "brief_revision_id": f"brief_20260620_daily_r{index + 1:03d}",
                }
            ),
        )

        for event in (
            {"event": "apply_started", "kind": "import", "run_id": import_id},
            {"event": "apply_completed", "kind": "import", "run_id": import_id},
            {"event": "apply_started", "kind": "briefing", "run_id": brief_id},
            {"event": "apply_completed", "kind": "briefing", "run_id": brief_id},
        ):
            secure_append(root / "logs" / "events.jsonl", canonical_json_bytes(event))
```

This fixture intentionally models 1,000 current projections and a bounded 20-run retained history. Increasing retained-history policy is a separate performance contract.

- [ ] **Step 4: Time only the phase workload, not workspace setup or teardown**

Refactor `worker_sample()` to use a phase-local measurement:

```python
def worker_sample(phase: str, *, records: int) -> dict[str, float]:
    if phase not in PHASES:
        raise ValueError(f"unknown phase: {phase}")
    with tempfile.TemporaryDirectory(prefix="opc-benchmark-") as temporary:
        root = Path(temporary)
        if phase == "status":
            _prepare_status_workspace(root, records=records)
        started = time.perf_counter()
        if phase == "normalize":
            for index in range(records):
                canonical_json_bytes(_synthetic_record(index))
        elif phase == "apply":
            destination = root / "data"
            for index in range(records):
                secure_replace(
                    destination / f"priority-{index:04d}.json",
                    canonical_json_bytes(_synthetic_record(index)),
                )
        elif phase == "briefing":
            candidates = (
                _candidate(
                    _synthetic_record(index),
                    today=date(2026, 6, 20),
                    thresholds={"CNY": "10000.00", "USD": "1500.00"},
                    last_evidence={},
                )
                for index in range(records)
            )
            sorted((item for item in candidates if item is not None), key=_ranking)
        else:
            workspace_status(root)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
    return {"elapsed_ms": elapsed_ms, "rss_mib": _rss_mib()}
```

- [ ] **Step 5: Run benchmark unit tests and a fast smoke sample**

Run:

```bash
uv run pytest tests/unit/test_benchmark.py -q
uv run python -m opc_ceo.benchmark --phase status --records 1000 --warmup 1 --repeat 3 --stat p95
```

Expected: tests pass; JSON output reports `passed: true`, status p95 below 2,000 ms, and p95 RSS below 100 MiB.

- [ ] **Step 6: Record a local checkpoint**

Run:

```bash
git diff --check
uv run ruff check src/opc_ceo/benchmark.py tests/unit/test_benchmark.py
uv run mypy src/opc_ceo/benchmark.py
```

Expected: all commands pass.

---

### Task 9: Close Branch Coverage And Run The Release Gate

**Files:**
- Modify: `tests/unit/test_status_reconstruction.py` only when coverage identifies an unexercised status branch
- Modify: `src/opc_ceo/diagnostics.py` only when a test demonstrates an implementation defect

- [ ] **Step 1: Run focused statement and branch coverage**

Run:

```bash
uv run pytest tests/unit/test_status_reconstruction.py \
  tests/unit/test_boundaries.py \
  tests/unit/test_cli_dispatch.py \
  --cov=opc_ceo.diagnostics \
  --cov-branch \
  --cov-report=term-missing \
  --cov-fail-under=100
```

Expected: 100% statements and branches for `opc_ceo.diagnostics`. A lower result blocks completion; do not add coverage exclusions.

- [ ] **Step 2: Run the complete test suite with the project coverage gate**

Run:

```bash
uv run pytest --cov=opc_ceo --cov-branch --cov-fail-under=100
```

Expected: all tests pass and total statement/branch coverage remains 100%.

- [ ] **Step 3: Run formatting, lint, typing, and generated-contract checks**

Run:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests evals spikes
uv run python -m opc_ceo.contracts generate --check
```

Expected: every command exits zero with no generated resource drift.

- [ ] **Step 4: Run the full status performance gate**

Run:

```bash
uv run python -m opc_ceo.benchmark \
  --phase status \
  --records 1000 \
  --warmup 3 \
  --repeat 20 \
  --stat p95
```

Expected: JSON reports `passed: true`, `p95_elapsed_ms < 2000`, and `p95_rss_mib < 100` on the approved baseline machine.

- [ ] **Step 5: Run a CLI smoke check against a fresh Workspace**

Run:

```bash
workspace="$(mktemp -d)/opc-workspace"
uv run opc-workspace --workspace "$workspace" --format json init --approve
uv run opc-workspace --workspace "$workspace" --format json status
```

Expected: initialization reports `initialized`; status reports `healthy` with all six new sections and zero counts.

- [ ] **Step 6: Audit the final serialized contract and repository state**

Run:

```bash
rg -n "record_id|title|amount|disposition|source_record" src/opc_ceo/diagnostics.py
git diff --check
git status --short
```

Expected: any matches are limited to local validation keys and never copied into returned aggregates or diagnostics; no whitespace errors; no staging or commits were created.

## Completion Criteria

The implementation is complete only when all of the following are true:

1. Empty and populated Workspaces return `imports`, `briefings`, `quarantine`, `recovery`, `cleanup`, and `audit`.
2. Every retained import and Briefing run contributes exactly one state count.
3. Malformed JSON, non-object JSON, malformed JSONL, contradictory artifacts, corrupt projections, duplicate completions, and missing audit files produce bounded typed diagnostics rather than crashes.
4. Unmatched Apply starts produce both legacy `pending_recovery` tokens and hashed `recovery.runs`, with final outcome `recovery_required`.
5. New sections expose no raw run IDs, revision IDs, record IDs, business values, or artifact contents.
6. Existing source redaction, cleanup behavior, CLI envelopes, and legacy recovery tests still pass.
7. Full statement and branch coverage is 100%.
8. The 1,000-record status benchmark passes below 2 seconds and 100 MiB p95.
