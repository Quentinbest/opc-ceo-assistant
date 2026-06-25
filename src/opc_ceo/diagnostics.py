from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from opc_ceo.contracts import CONTRACT_VERSION
from opc_ceo.workspace import (
    DIRECTORIES,
    WorkspaceError,
    load_manifest,
    resolve_workspace_path,
    secure_unlink,
)

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


def cleanup_expired_raw_copies(
    root: Path, *, now: datetime | None = None
) -> tuple[int, list[dict[str, Any]]]:
    summary, diagnostics = _cleanup_raw_copies(root, now=now)
    return summary["deleted_raw_copies"], diagnostics


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


def validate_workspace(root: Path) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    manifest_path = root / ".opc" / "manifest.json"
    if not manifest_path.is_file():
        return [{"code": "WORKSPACE_NOT_INITIALIZED", "path": str(manifest_path)}]
    manifest = load_manifest(root)
    if manifest.get("schema_version") != CONTRACT_VERSION:
        diagnostics.append({"code": "UNSUPPORTED_VERSION", "path": ".opc/manifest.json"})
    mirrors = {
        "schema_sha256": (
            ".opc/schemas/1.0.0/opc-workspace.schema.json",
            "SCHEMA_MIRROR_DRIFT",
        ),
        "workbook_contract_sha256": (
            ".opc/schemas/1.0.0/workbook-contract.json",
            "CONTRACT_MIRROR_DRIFT",
        ),
    }
    for manifest_key, (relative, code) in mirrors.items():
        mirror = resolve_workspace_path(root, relative)
        actual = hashlib.sha256(mirror.read_bytes()).hexdigest() if mirror.is_file() else None
        if actual != manifest.get(manifest_key):
            diagnostics.append({"code": code, "path": relative})
    for relative in (".", *DIRECTORIES):
        path = root if relative == "." else resolve_workspace_path(root, relative)
        if not path.is_dir():
            diagnostics.append({"code": "MISSING_DIRECTORY", "path": relative})
        elif stat.S_IMODE(path.stat().st_mode) != 0o700:
            diagnostics.append({"code": "INSECURE_DIRECTORY_MODE", "path": relative})
    if stat.S_IMODE(manifest_path.stat().st_mode) != 0o600:
        diagnostics.append({"code": "INSECURE_FILE_MODE", "path": ".opc/manifest.json"})
    for raw in sorted((root / "inbox" / "imports").glob("*/source.xlsx")):
        diagnostics.append({"code": "RAW_RETENTION_PENDING", "path": str(raw.relative_to(root))})
    return sorted(diagnostics, key=lambda item: (item["code"], item["path"]))


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
    status_artifacts = (
        "envelope.json",
        "normalized.jsonl",
        "diff.json",
        "diagnostics.json",
        "resolution.json",
        "apply_plan.json",
        "seal.json",
        "apply_result.json",
    )
    if not imports_root.is_dir():
        return summary, diagnostics
    for run_root in sorted(path for path in imports_root.iterdir() if path.is_dir()):
        if not any((run_root / name).exists() for name in status_artifacts):
            continue
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
            sealed_shape_valid = not has_sealed_set or (
                seal is not None
                and isinstance(seal.get("seal_sha256"), str)
                and brief is not None
                and brief.get("brief_date") == brief_date
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
                    "revision_id_hash": _fingerprint(revision_id)
                    if isinstance(revision_id, str)
                    else None,
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
        if pending is not None and not isinstance(pending, dict):
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
            if domain is None:
                diagnostics.append(
                    {
                        "code": "PROJECTION_STATUS_ARTIFACT_ERROR",
                        "path": _status_path(root, path),
                        "reason": "invalid_shape",
                    }
                )
                summary["corrupt_projections"] += 1
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
        if domain is None:
            diagnostics.append(
                {
                    "code": "PROJECTION_STATUS_ARTIFACT_ERROR",
                    "path": _status_path(root, path),
                    "reason": "invalid_shape",
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


def _diagnostic_key(item: dict[str, Any]) -> tuple[str, str, int, str]:
    return (
        str(item.get("code", "")),
        str(item.get("path", "")),
        int(item.get("line", 0)),
        str(item.get("reason", "")),
    )


def workspace_status(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    cleanup, cleanup_diagnostics = _cleanup_raw_copies(root, now=now)
    diagnostics = validate_workspace(root)
    diagnostics.extend(cleanup_diagnostics)
    manifest = (
        load_manifest(root)
        if not any(d["code"] == "WORKSPACE_NOT_INITIALIZED" for d in diagnostics)
        else {}
    )
    imports, import_diagnostics = _summarize_imports(root)
    diagnostics.extend(import_diagnostics)
    briefings, briefing_diagnostics = _summarize_briefings(root)
    diagnostics.extend(briefing_diagnostics)
    quarantine, projection_diagnostics = _summarize_quarantine(root)
    diagnostics.extend(projection_diagnostics)
    audit, recovery_pairs, audit_diagnostics = _summarize_audit(root)
    diagnostics.extend(audit_diagnostics)
    pending_recovery = [f"{kind}:{run_id}" for kind, run_id in recovery_pairs]
    recovery = {
        "required": len(recovery_pairs),
        "runs": [f"{kind}:{_fingerprint(run_id)}" for kind, run_id in recovery_pairs],
    }
    if recovery_pairs:
        diagnostics.append({"code": "PARTIAL_APPLY_RECOVERY_REQUIRED", "path": pending_recovery[0]})
    source = manifest.get("source")
    if isinstance(source, dict):
        source = dict(source)
        spreadsheet_id = source.pop("spreadsheet_id", None)
        source["spreadsheet_id_configured"] = bool(spreadsheet_id)
        if spreadsheet_id:
            source["spreadsheet_id_hash"] = _fingerprint(str(spreadsheet_id))
    diagnostics.sort(key=_diagnostic_key)
    outcome = "recovery_required" if recovery_pairs else ("degraded" if diagnostics else "healthy")
    return {
        "source": source,
        "diagnostics": diagnostics,
        "imports": imports,
        "briefings": briefings,
        "quarantine": quarantine,
        "pending_recovery": pending_recovery,
        "recovery": recovery,
        "audit": audit,
        "cleanup": cleanup,
        "outcome": outcome,
    }


def verify_connector_receipt(path: Path) -> tuple[bool, list[dict[str, Any]]]:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        capabilities = receipt["capabilities"]
        export = capabilities["xlsx_export"]
        cleanup = capabilities["host_attachment_cleanup"]
        expected = {
            "opc_priorities_v1",
            "opc_pipeline_v1",
            "opc_receivables_v1",
            "opc_contracts_v1",
            "opc_projects_v1",
            "opc_risks_v1",
        }
        valid = (
            receipt["status"] == "passed"
            and capabilities["raw_cells_entered_host_context"] is False
            and set(export["named_ranges"]) == expected
            and export["absolute_local_path"] is True
            and export["python_read"]["succeeded"] is True
            and cleanup["owner"] == "host"
            and (cleanup["automatic_cleanup"] is True or cleanup["safe_disposable_path"] is True)
            and str(capabilities["drive_metadata"]["version"]).isdigit()
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        return False, [{"code": "CONNECTOR_CAPABILITY_ERROR", "message": str(error)}]
    return (True, []) if valid else (False, [{"code": "CONNECTOR_CAPABILITY_ERROR"}])
