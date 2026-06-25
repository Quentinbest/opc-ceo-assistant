from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opc_ceo.contracts import canonical_json_bytes
from opc_ceo.diagnostics import (
    _cleanup_raw_copies,
    _count_status_objects,
    _parse_status_datetime,
    _projection_domain,
    _read_status_object,
    _summarize_audit,
    _summarize_briefings,
    _summarize_imports,
    _summarize_quarantine,
    _valid_status_date,
    validate_workspace,
    workspace_status,
)
from opc_ceo.workspace import initialize_workspace, secure_replace, secure_write

NOW = datetime(2026, 6, 20, 8, 0, tzinfo=UTC)


def initialized_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    assert initialize_workspace(root, approved=True) == "initialized"
    return root


def write_json(path: Path, value: Any) -> None:
    secure_replace(path, canonical_json_bytes(value))


def _fingerprint_for_test(value: str) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


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

    _, object_diagnostic = _read_status_object(root, non_object, "IMPORT_STATUS_ARTIFACT_ERROR")
    _, link_diagnostic = _read_status_object(root, linked, "IMPORT_STATUS_ARTIFACT_ERROR")

    assert object_diagnostic is not None
    assert object_diagnostic["reason"] == "not_object"
    assert link_diagnostic is not None
    assert link_diagnostic["reason"] == "invalid_shape"
    assert "outside" not in json.dumps(link_diagnostic)


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
    import_envelope(root, "run-clean", exported_at="2026-06-20T00:00:00Z")
    import_envelope(root, "run-degraded", exported_at="2026-06-20T01:00:00Z", degraded=True)
    sealed = import_envelope(root, "run-sealed", exported_at="2026-06-20T02:00:00Z")
    write_json(sealed / "seal.json", {"seal_sha256": "seal"})
    applied = import_envelope(
        root, "run-z-applied", exported_at="2026-06-20T02:00:00Z", drive_version="9"
    )
    write_json(applied / "seal.json", {"seal_sha256": "seal"})
    write_json(applied / "apply_result.json", {"outcome": "applied"})
    import_envelope(root, "zz-older", exported_at="2026-06-19T23:00:00Z")

    result = workspace_status(root)

    assert result["imports"]["runs"] == 5
    assert result["imports"]["states"] == {
        "staged_clean": 2,
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
    briefing_candidate(root, "zz-older", brief_date="2026-06-18")

    result = workspace_status(root)

    assert result["briefings"]["runs"] == 4
    assert result["briefings"]["states"] == {
        "drafted": 2,
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
    audit_errors = [
        item for item in result["diagnostics"] if item["code"] == "AUDIT_LOG_LINE_ERROR"
    ]
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


def test_new_status_sections_never_expose_business_values_or_raw_ids(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)
    run_root = import_envelope(root, "import-private-raw-id", exported_at="2026-06-20T00:00:00Z")
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


def test_reader_and_parser_error_paths_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = initialized_workspace(tmp_path)
    target = root / "inbox" / "imports" / "run" / "envelope.json"
    target.parent.mkdir(mode=0o700)
    secure_write(target, b"{}", exclusive=True)

    original_lstat = Path.lstat
    original_read_text = Path.read_text

    def raising_lstat(path: Path) -> os.stat_result:
        if path == target:
            raise OSError("boom")
        return original_lstat(path)

    def raising_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == target:
            raise OSError("boom")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", raising_lstat)
    _, diagnostic = _read_status_object(root, target, "IMPORT_STATUS_ARTIFACT_ERROR")
    assert diagnostic is not None and diagnostic["reason"] == "unreadable"

    monkeypatch.setattr(Path, "lstat", original_lstat)
    monkeypatch.setattr(Path, "read_text", raising_read_text)
    _, diagnostic = _read_status_object(root, target, "IMPORT_STATUS_ARTIFACT_ERROR")
    assert diagnostic is not None and diagnostic["reason"] == "unreadable"

    directory = root / "inbox" / "imports" / "dir"
    directory.mkdir(mode=0o700)
    _, diagnostic = _read_status_object(root, directory, "IMPORT_STATUS_ARTIFACT_ERROR")
    assert diagnostic is not None and diagnostic["reason"] == "invalid_shape"

    assert _parse_status_datetime(123) is None
    assert _parse_status_datetime("invalid") is None
    assert _parse_status_datetime("2026-06-20T00:00:00") is None
    assert not _valid_status_date(123)
    assert not _valid_status_date("2026-99-99")


def test_cleanup_helper_and_validation_cover_error_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing"
    summary, diagnostics = _cleanup_raw_copies(missing, now=NOW)
    assert summary["deleted_raw_copies"] == 0
    assert diagnostics == []

    root = initialized_workspace(tmp_path)
    bad_stat = root / "inbox" / "imports" / "bad-stat" / "source.xlsx"
    bad_stat.parent.mkdir(mode=0o700)
    secure_write(bad_stat, b"xlsx", exclusive=True)
    bad_unlink = root / "inbox" / "imports" / "bad-unlink" / "source.xlsx"
    bad_unlink.parent.mkdir(mode=0o700)
    secure_write(bad_unlink, b"xlsx", exclusive=True)
    for path in (bad_stat, bad_unlink):
        timestamp = (NOW - timedelta(hours=169)).timestamp()
        os.utime(path, (timestamp, timestamp))

    original_lstat = Path.lstat

    def patched_lstat(path: Path) -> os.stat_result:
        if path == bad_stat:
            raise OSError("stat fail")
        return original_lstat(path)

    def patched_unlink(path: Path) -> None:
        if path == bad_unlink:
            raise OSError("unlink fail")
        path.unlink(missing_ok=True)

    monkeypatch.setattr(Path, "lstat", patched_lstat)
    monkeypatch.setattr("opc_ceo.diagnostics.secure_unlink", patched_unlink)
    summary, diagnostics = _cleanup_raw_copies(root, now=NOW)
    assert summary["cleanup_errors"] == 2
    assert {item["reason"] for item in diagnostics} == {"unreadable"}

    manifest_path = root / ".opc" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_sha256"] = "bad"
    manifest["workbook_contract_sha256"] = "bad"
    secure_replace(manifest_path, canonical_json_bytes(manifest))
    codes = {item["code"] for item in validate_workspace(root)}
    assert {"SCHEMA_MIRROR_DRIFT", "CONTRACT_MIRROR_DRIFT"} <= codes


def test_import_summarizer_covers_error_states(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)
    missing = root / "inbox" / "imports" / "missing-envelope"
    missing.mkdir(mode=0o700)
    write_json(missing / "apply_result.json", {"outcome": "applied"})

    invalid = root / "inbox" / "imports" / "invalid-envelope"
    write_json(invalid / "envelope.json", {"exported_at": "bad", "degraded": "no", "drive": {}})

    bad_seal = import_envelope(root, "bad-seal", exported_at="2026-06-20T00:00:00Z")
    write_json(bad_seal / "seal.json", {"seal_sha256": 1})

    contradictory = import_envelope(root, "contradictory", exported_at="2026-06-20T01:00:00Z")
    write_json(contradictory / "apply_result.json", {"outcome": "applied"})

    corrupt_artifact = import_envelope(root, "corrupt-artifact", exported_at="2026-06-20T02:00:00Z")
    secure_write(corrupt_artifact / "seal.json", b"{broken", exclusive=True)

    summary, diagnostics = _summarize_imports(root)

    assert summary["runs"] == 5
    assert summary["states"]["blocked_or_corrupt"] == 5
    reasons = {item["reason"] for item in diagnostics}
    assert {"missing_file", "invalid_shape", "contradictory_artifacts", "invalid_json"} <= reasons


def test_briefing_summarizer_and_counters_cover_error_states(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)
    missing = root / "inbox" / "briefings" / "missing-candidate"
    missing.mkdir(mode=0o700)
    write_json(missing / "seal.json", {"seal_sha256": "seal"})

    corrupt = root / "inbox" / "briefings" / "corrupt-candidate"
    corrupt.mkdir(mode=0o700)
    secure_write(corrupt / "candidate_set.json", b"{broken", exclusive=True)

    invalid_date = briefing_candidate(root, "invalid-date", brief_date="bad-date")

    bad_shape = briefing_candidate(root, "bad-shape", brief_date="2026-06-20")
    write_json(bad_shape / "brief.json", {"brief_date": "2026-06-19"})
    write_json(bad_shape / "dispositions.json", {})
    write_json(bad_shape / "seal.json", {"seal_sha256": "seal"})

    contradictory = briefing_candidate(root, "contradictory", brief_date="2026-06-20")
    write_json(
        contradictory / "apply_result.json",
        {"outcome": "applied", "brief_revision_id": "brief_20260620_daily_r001"},
    )

    partial = briefing_candidate(root, "partial", brief_date="2026-06-20")
    write_json(partial / "brief.json", {"brief_date": "2026-06-20"})

    counter_path = root / "data" / "briefs" / "brief_20260620_daily_r001.json"
    write_json(counter_path, {"wrong": "shape"})

    summary, diagnostics = _summarize_briefings(root)
    assert summary["states"]["blocked_or_corrupt"] == 6
    reasons = {item["reason"] for item in diagnostics}
    assert {"missing_file", "invalid_json", "invalid_shape", "contradictory_artifacts"} <= reasons

    count, diagnostics = _count_status_objects(
        root,
        [counter_path],
        "BRIEF_STATUS_ARTIFACT_ERROR",
        "brief_revision_id",
    )
    assert count == 0
    assert diagnostics[0]["reason"] == "invalid_shape"
    assert invalid_date.exists()


def test_briefing_summarizer_tolerates_empty_private_reader_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = initialized_workspace(tmp_path)
    run_root = briefing_candidate(root, "reader-gap", brief_date="2026-06-20")
    write_json(run_root / "seal.json", {"seal_sha256": "seal"})

    original = _read_status_object

    def patched_reader(
        workspace_root: Path, path: Path, code: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if path == run_root / "seal.json":
            return None, None
        return original(workspace_root, path, code)

    monkeypatch.setattr("opc_ceo.diagnostics._read_status_object", patched_reader)
    summary, diagnostics = _summarize_briefings(root)
    assert summary["states"]["drafted"] == 1
    assert diagnostics == []


def test_quarantine_and_projection_helpers_cover_error_branches(tmp_path: Path) -> None:
    root = initialized_workspace(tmp_path)
    assert _projection_domain(1) is None

    secure_write(root / "data" / "source_projection" / "bad-json.json", b"{broken", exclusive=True)
    write_json(
        root / "data" / "source_projection" / "bad-pending.json",
        {"record_id": "priority_bad", "pending": []},
    )
    write_json(
        root / "data" / "source_projection" / "no-domain-empty.json",
        {"record_id": "mystery", "pending": None},
    )
    write_json(
        root / "data" / "source_projection" / "no-domain-rejected.json",
        {"record_id": "mystery", "pending": {"status": "rejected"}},
    )

    summary, diagnostics = _summarize_quarantine(root)
    assert summary["corrupt_projections"] == 4
    reasons = {item["reason"] for item in diagnostics}
    assert {"invalid_json", "invalid_shape"} <= reasons


def test_audit_helper_covers_missing_invalid_and_unreadable_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = initialized_workspace(tmp_path)
    event_path = root / "logs" / "events.jsonl"
    event_path.unlink()

    summary, recovery, diagnostics = _summarize_audit(root)
    assert summary["valid_events"] == 0
    assert recovery == []
    assert diagnostics[0]["reason"] == "missing_file"

    event_path.mkdir(mode=0o700)
    summary, recovery, diagnostics = _summarize_audit(root)
    assert diagnostics[0]["reason"] == "invalid_shape"
    event_path.rmdir()

    secure_write(
        event_path,
        canonical_json_bytes({"event": "apply_started", "kind": "import", "run_id": "r1"}),
        exclusive=True,
    )
    original_lstat = Path.lstat
    original_read_text = Path.read_text

    def raising_lstat(path: Path) -> os.stat_result:
        if path == event_path:
            raise OSError("boom")
        return original_lstat(path)

    def raising_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == event_path:
            raise OSError("boom")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", raising_lstat)
    _, _, diagnostics = _summarize_audit(root)
    assert diagnostics[0]["reason"] == "unreadable"

    monkeypatch.setattr(Path, "lstat", original_lstat)
    monkeypatch.setattr(Path, "read_text", raising_read_text)
    _, _, diagnostics = _summarize_audit(root)
    assert diagnostics[0]["reason"] == "unreadable"

    monkeypatch.setattr(Path, "read_text", original_read_text)
    write_events(root, [b" ", b"[]"])
    summary, _, diagnostics = _summarize_audit(root)
    assert summary["malformed_events"] == 1
    assert diagnostics[0]["reason"] == "invalid_shape"
