from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from opc_ceo.contracts import CONTRACT_VERSION, canonical_json_bytes, load_contract
from opc_ceo.workspace import (
    FaultPoint,
    ensure_supported_workspace,
    event_exists,
    fault_checkpoint,
    load_manifest,
    resolve_workspace_path,
    secure_append,
    secure_mkdir,
    secure_replace,
    secure_unlink,
    secure_write,
    secure_write_once,
    workspace_lock,
)

ID_PATTERN = re.compile(
    r"^(priority|pipeline|receivable|contract|project|risk)_[a-z0-9][a-z0-9_-]{2,63}$"
)
DATE_FIELDS = {
    "target_date",
    "next_action_due",
    "due_date",
    "review_date",
    "signature_date",
    "renewal_date",
    "milestone_due",
    "mitigation_date",
    "archived_at",
}
AMOUNT_FIELDS = {"amount", "total_amount", "outstanding_amount"}
BOOLEAN_FIELDS = {"disputed", "blocked"}
INTEGER_FIELDS = {"weight", "strategic_weight"}
DOMAIN_DIRECTORIES = {
    "priority": "priorities",
    "pipeline": "pipeline",
    "receivable": "receivables",
    "contract": "contracts",
    "project": "projects",
    "risk": "risks",
}


class IntakeError(ValueError):
    pass


class ApprovalMismatch(IntakeError):
    pass


@dataclass(frozen=True)
class ConnectorMetadata:
    version: str
    modified_time: str


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise IntakeError(f"expected JSON object: {path.name}")
    return value


def _metadata(value: object) -> ConnectorMetadata:
    if not isinstance(value, dict):
        raise IntakeError("metadata must be an object")
    version = value.get("version")
    modified_time = value.get("modifiedTime")
    if not isinstance(version, str) or not version.isdigit() or not isinstance(modified_time, str):
        raise IntakeError("metadata requires numeric version and modifiedTime")
    return ConnectorMetadata(version, modified_time)


def _run_id(now: datetime, source_hash: str) -> str:
    return f"import_{now.astimezone(UTC):%Y%m%dT%H%M%SZ}_{source_hash[:10]}"


def _copy_external_once(source: Path, destination: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise IntakeError("Connector artifact is not a regular file")
        if details.st_uid != os.getuid():
            raise IntakeError("Connector artifact owner does not match current user")
        if details.st_size <= 0 or details.st_size > 100 * 1024 * 1024:
            raise IntakeError("Connector artifact size is invalid")
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            chunks.append(chunk)
        secure_write(destination, b"".join(chunks), exclusive=True)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _text(value: object) -> str:
    return unicodedata.normalize("NFC", str(value).strip())


def _date_string(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(_text(value)).isoformat()


def _timestamp(value: object) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires an offset")
    return parsed.isoformat()


def _normalize_value(field: str, value: object) -> object:
    if value is None or value == "":
        return None
    if field == "updated_at":
        return _timestamp(value)
    if field in DATE_FIELDS:
        return _date_string(value)
    if field in AMOUNT_FIELDS:
        decimal = Decimal(_text(value))
        if not decimal.is_finite():
            raise InvalidOperation
        return format(decimal, "f")
    if field == "currency":
        currency = _text(value).upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("currency must be ISO 4217")
        return currency
    if field in BOOLEAN_FIELDS:
        if isinstance(value, bool):
            return value
        normalized = _text(value).lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
        raise ValueError("invalid boolean")
    if field in INTEGER_FIELDS:
        integer = int(_text(value))
        if not 1 <= integer <= 5:
            raise ValueError("weight must be 1 through 5")
        return integer
    return _text(value)


def _defined_range(workbook: Any, name: str) -> tuple[str, str]:
    definition = workbook.defined_names.get(name)
    if definition is None:
        raise IntakeError(f"missing named range: {name}")
    destinations = list(definition.destinations)
    if len(destinations) != 1:
        raise IntakeError(f"invalid named range: {name}")
    return str(destinations[0][0]), str(destinations[0][1])


def _normalize_workbook(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    contract = load_contract()
    workbook = load_workbook(path, read_only=True, data_only=False)
    normalized: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    seen: set[str] = set()
    identity_failure = False
    try:
        for tab in contract["tabs"]:
            record_type = str(tab["type"])
            sheet = str(tab["sheet"])
            range_name = str(tab["named_range"])
            columns = list(tab["columns"])
            actual_sheet, coordinates = _defined_range(workbook, range_name)
            if actual_sheet != sheet:
                raise IntakeError(f"named range {range_name} points to {actual_sheet}")
            worksheet = workbook[sheet]
            cells = worksheet[coordinates]
            headers = [cell.value for cell in cells[0]]
            if headers != columns:
                raise IntakeError(f"header drift in {range_name}")
            for row_number, cells_row in enumerate(cells[1:], 2):
                if all(cell.value in (None, "") for cell in cells_row):
                    continue
                identity_cell = cells_row[0]
                raw_id = identity_cell.value
                record_id = "" if raw_id is None else _text(raw_id)
                if (
                    identity_cell.data_type == "f"
                    or not ID_PATTERN.fullmatch(record_id)
                    or not record_id.startswith(f"{record_type}_")
                    or record_id in seen
                ):
                    identity_failure = True
                    diagnostics.append(
                        {
                            "code": "IDENTITY_CONTRACT_ERROR",
                            "record_id": record_id or None,
                            "path": f"{sheet}:{row_number}",
                        }
                    )
                    continue
                seen.add(record_id)
                row: dict[str, Any] = {"type": record_type}
                row_errors: list[str] = []
                for field, cell in zip(columns, cells_row, strict=True):
                    if cell.data_type == "f":
                        row_errors.append(field)
                        continue
                    try:
                        row[field] = _normalize_value(field, cell.value)
                    except (ValueError, TypeError, InvalidOperation):
                        row_errors.append(field)
                if not row.get("title") or not row.get("status") or not row.get("updated_at"):
                    row_errors.append("required")
                if row_errors:
                    diagnostics.append(
                        {
                            "code": "ROW_QUARANTINED",
                            "record_id": record_id,
                            "path": f"{sheet}:{row_number}",
                            "fields": sorted(set(row_errors)),
                        }
                    )
                    normalized.append(
                        {"type": record_type, "record_id": record_id, "quarantined": True}
                    )
                else:
                    row["quarantined"] = False
                    normalized.append(row)
    finally:
        workbook.close()
    normalized.sort(key=lambda item: (item["type"], item["record_id"]))
    diagnostics.sort(key=lambda item: (item["code"], item.get("record_id") or "", item["path"]))
    return normalized, diagnostics, identity_failure


def _projection(root: Path, record_id: str) -> dict[str, Any] | None:
    path = resolve_workspace_path(root, f"data/source_projection/{record_id}.json")
    return _read_json(path) if path.exists() else None


def _canonical_record(root: Path, record_type: str, record_id: str) -> dict[str, Any] | None:
    path = resolve_workspace_path(root, f"data/{DOMAIN_DIRECTORIES[record_type]}/{record_id}.json")
    return _read_json(path) if path.exists() else None


def _amount_anomaly(
    previous: dict[str, Any], incoming: dict[str, Any], thresholds: dict[str, str]
) -> bool:
    fields = {
        "priority": ("amount",),
        "pipeline": ("amount",),
        "receivable": ("total_amount", "outstanding_amount"),
    }.get(str(incoming["type"]), ())
    if not fields:
        return False
    old_currency = previous.get("currency")
    new_currency = incoming.get("currency")
    if old_currency != new_currency:
        return True
    if new_currency is None:
        return False
    if new_currency not in thresholds:
        raise IntakeError(f"missing approval threshold for {new_currency}")
    threshold = Decimal(thresholds[new_currency])
    for field in fields:
        old = Decimal(previous.get(field) or "0")
        new = Decimal(incoming.get(field) or "0")
        delta = abs(new - old)
        if old == 0 or new == 0:
            if delta > threshold:
                return True
        elif delta > threshold and max(abs(old), abs(new)) / min(abs(old), abs(new)) >= 3:
            return True
    return False


def _build_diff(root: Path, rows: list[dict[str, Any]], degraded: bool) -> list[dict[str, Any]]:
    diff: list[dict[str, Any]] = []
    incoming_ids: set[str] = set()
    thresholds = load_manifest(root)["approval_amount_thresholds"]
    for row in rows:
        record_id = str(row["record_id"])
        incoming_ids.add(record_id)
        if row.get("quarantined"):
            diff.append({"record_id": record_id, "type": row["type"], "status": "quarantined"})
            continue
        source_hash = _hash(canonical_json_bytes(row))
        projection = _projection(root, record_id)
        applied = None if projection is None else projection.get("applied")
        applied_hash = None if not applied else applied.get("source_hash")
        if source_hash == applied_hash:
            status = "unchanged"
        elif not applied:
            status = "add"
        else:
            canonical = _canonical_record(root, str(row["type"]), record_id)
            canonical_hash = None if canonical is None else _hash(canonical_json_bytes(canonical))
            if canonical_hash != applied.get("canonical_hash"):
                status = "conflict"
            elif _amount_anomaly(applied.get("source_record", canonical or {}), row, thresholds):
                status = "amount_anomaly"
            else:
                status = "change"
        diff.append(
            {
                "record_id": record_id,
                "type": row["type"],
                "status": status,
                "source_hash": source_hash,
            }
        )
    if not degraded:
        projection_root = resolve_workspace_path(root, "data/source_projection")
        for path in sorted(projection_root.glob("*.json")):
            record_id = path.stem
            if record_id not in incoming_ids:
                projection = _read_json(path)
                diff.append(
                    {
                        "record_id": record_id,
                        "type": record_id.split("_", 1)[0],
                        "status": "tombstone_pending",
                        "source_hash": projection["applied"]["source_hash"],
                    }
                )
    return sorted(diff, key=lambda item: (item["type"], item["record_id"]))


def stage_import(
    root: Path, source: Path, metadata: dict[str, object], *, now: datetime
) -> dict[str, Any]:
    try:
        before = _metadata(metadata.get("before"))
        after = _metadata(metadata.get("after"))
    except IntakeError as error:
        return {
            "outcome": "blocked",
            "run_id": None,
            "diagnostics": [{"code": "CONNECTOR_METADATA_ERROR", "message": str(error)}],
        }
    if before != after:
        return {
            "outcome": "blocked",
            "run_id": None,
            "diagnostics": [{"code": "SOURCE_CHANGED_DURING_EXPORT"}],
        }
    ensure_supported_workspace(root)
    manifest = load_manifest(root)
    if manifest["source"].get("last_fully_applied_drive_version") == before.version:
        manifest["source"]["last_successful_refresh_at"] = now.astimezone(UTC).isoformat()
        secure_replace(root / ".opc" / "manifest.json", canonical_json_bytes(manifest))
        return {"outcome": "unchanged", "run_id": None, "diagnostics": []}

    preliminary = hashlib.sha256(f"{source}:{before.version}".encode()).hexdigest()
    run_id = _run_id(now, preliminary)
    run_root = resolve_workspace_path(root, f"inbox/imports/{run_id}")
    if run_root.exists():
        suffix = hashlib.sha256(str(now.timestamp()).encode()).hexdigest()[:4]
        run_id = f"{run_id}_{suffix}"
        run_root = resolve_workspace_path(root, f"inbox/imports/{run_id}")
    secure_mkdir(run_root)
    staged_source = run_root / "source.xlsx"
    try:
        export_hash = _copy_external_once(source, staged_source)
        rows, diagnostics, identity_failure = _normalize_workbook(staged_source)
    except (OSError, IntakeError, KeyError, ValueError, zipfile.BadZipFile) as error:
        return {
            "outcome": "blocked",
            "run_id": run_id,
            "diagnostics": [{"code": "WORKBOOK_CONTRACT_ERROR", "message": str(error)}],
        }

    if identity_failure:
        return {"outcome": "blocked", "run_id": run_id, "diagnostics": diagnostics}
    degraded = any(item["code"] == "ROW_QUARANTINED" for item in diagnostics)
    diff = _build_diff(root, rows, degraded)
    normalized_bytes = b"".join(canonical_json_bytes(row) for row in rows)
    envelope = {
        "contract_version": CONTRACT_VERSION,
        "run_id": run_id,
        "drive": {
            "version": before.version,
            "modifiedTime": before.modified_time,
        },
        "spreadsheet_id_hash": metadata.get("spreadsheet_id_hash"),
        "exported_at": now.astimezone(UTC).isoformat(),
        "export_sha256": export_hash,
        "normalized_sha256": _hash(normalized_bytes),
        "complete": True,
        "degraded": degraded,
        "row_count": len(rows),
    }
    secure_write(run_root / "normalized.jsonl", normalized_bytes, exclusive=True)
    secure_write(run_root / "envelope.json", canonical_json_bytes(envelope), exclusive=True)
    secure_write(run_root / "diff.json", canonical_json_bytes(diff), exclusive=True)
    secure_write(run_root / "diagnostics.json", canonical_json_bytes(diagnostics), exclusive=True)
    secure_unlink(staged_source)
    manifest["source"].update(
        {
            "last_observed_drive_version": before.version,
            "last_observed_modified_time": before.modified_time,
            "last_successful_refresh_at": now.astimezone(UTC).isoformat(),
        }
    )
    secure_replace(root / ".opc" / "manifest.json", canonical_json_bytes(manifest))
    return {
        "outcome": "staged_degraded" if degraded else "staged_clean",
        "run_id": run_id,
        "diagnostics": diagnostics,
    }


def review_import(root: Path, run_id: str) -> dict[str, Any]:
    run_root = resolve_workspace_path(root, f"inbox/imports/{run_id}")
    diff = json.loads((run_root / "diff.json").read_text(encoding="utf-8"))
    groups: dict[str, list[str]] = {
        "batch_safe": [],
        "quarantined": [],
        "conflicts": [],
        "tombstones": [],
        "anomalies": [],
    }
    for item in diff:
        status = item["status"]
        if status in {"add", "change"}:
            groups["batch_safe"].append(item["record_id"])
        elif status == "quarantined":
            groups["quarantined"].append(item["record_id"])
        elif status == "tombstone_pending":
            groups["tombstones"].append(item["record_id"])
        elif status == "conflict":
            groups["conflicts"].append(item["record_id"])
        elif status == "amount_anomaly":
            groups["anomalies"].append(item["record_id"])
    return groups


def _load_rows(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_bytes().splitlines():
        row = json.loads(line)
        rows[str(row["record_id"])] = row
    return rows


def _seal(run_root: Path, paths: list[str], preconditions: dict[str, Any]) -> dict[str, Any]:
    entries = [
        {"path": name, "sha256": _hash((run_root / name).read_bytes())} for name in sorted(paths)
    ]
    body = {"schema_version": "1.0.0", "entries": entries, "preconditions": preconditions}
    seal_hash = _hash(canonical_json_bytes(body))
    return {**body, "seal_sha256": seal_hash}


def resolve_import(root: Path, run_id: str, resolution: dict[str, Any]) -> dict[str, Any]:
    ensure_supported_workspace(root)
    run_root = resolve_workspace_path(root, f"inbox/imports/{run_id}")
    diff = json.loads((run_root / "diff.json").read_text(encoding="utf-8"))
    batch = resolution.get("batch")
    items = resolution.get("items", {})
    if batch not in {"approve", "reject"} or not isinstance(items, dict):
        raise IntakeError("resolution requires batch approve/reject and items object")
    decisions: list[dict[str, str]] = []
    preconditions: dict[str, Any] = {}
    for item in diff:
        record_id = str(item["record_id"])
        status = str(item["status"])
        decision: str
        if status in {"quarantined", "unchanged"}:
            decision = "pending" if status == "quarantined" else "unchanged"
        elif status in {"conflict", "tombstone_pending", "amount_anomaly"}:
            selected = items.get(record_id)
            if selected not in {"approve", "reject"}:
                raise IntakeError(f"item decision required: {record_id}")
            decision = str(selected)
        else:
            selected = items.get(record_id, batch)
            if selected not in {"approve", "reject"}:
                raise IntakeError(f"invalid item decision: {record_id}")
            decision = str(selected)
        projection = _projection(root, record_id)
        canonical = _canonical_record(root, str(item["type"]), record_id)
        preconditions[record_id] = {
            "projection": None if projection is None else _hash(canonical_json_bytes(projection)),
            "canonical": None if canonical is None else _hash(canonical_json_bytes(canonical)),
        }
        decisions.append({"record_id": record_id, "decision": decision, "status": status})
    canonical_resolution = {"batch": batch, "items": dict(sorted(items.items()))}
    apply_plan = {"run_id": run_id, "decisions": decisions}
    secure_write(
        run_root / "resolution.json", canonical_json_bytes(canonical_resolution), exclusive=True
    )
    secure_write(run_root / "apply_plan.json", canonical_json_bytes(apply_plan), exclusive=True)
    seal = _seal(
        run_root,
        ["envelope.json", "normalized.jsonl", "diff.json", "resolution.json", "apply_plan.json"],
        preconditions,
    )
    secure_write(run_root / "seal.json", canonical_json_bytes(seal), exclusive=True)
    return {
        "outcome": "sealed",
        "run_id": run_id,
        "approval_token": f"{run_id}:{seal['seal_sha256']}",
        "review": apply_plan,
    }


def _verify_seal(run_root: Path, seal: dict[str, Any]) -> None:
    expected = _seal(
        run_root,
        [entry["path"] for entry in seal["entries"]],
        seal["preconditions"],
    )
    if expected != seal:
        raise ApprovalMismatch("sealed files changed")


def _verify_preconditions(root: Path, preconditions: dict[str, Any]) -> None:
    for record_id, expected in preconditions.items():
        projection = _projection(root, record_id)
        record_type = record_id.split("_", 1)[0]
        canonical = _canonical_record(root, record_type, record_id)
        actual = {
            "projection": None if projection is None else _hash(canonical_json_bytes(projection)),
            "canonical": None if canonical is None else _hash(canonical_json_bytes(canonical)),
        }
        if actual != expected:
            raise ApprovalMismatch(f"precondition changed: {record_id}")


def apply_import(root: Path, run_id: str, *, confirm: str, now: datetime) -> dict[str, Any]:
    ensure_supported_workspace(root)
    run_root = resolve_workspace_path(root, f"inbox/imports/{run_id}")
    seal = _read_json(run_root / "seal.json")
    expected_token = f"{run_id}:{seal['seal_sha256']}"
    if confirm != expected_token:
        raise ApprovalMismatch("approval token mismatch")
    result_path = run_root / "apply_result.json"
    if result_path.exists():
        if not event_exists(root, "apply_completed", "import", run_id):
            secure_append(
                root / "logs" / "events.jsonl",
                canonical_json_bytes(
                    {
                        "event": "apply_completed",
                        "kind": "import",
                        "run_id": run_id,
                        "at": now.astimezone(UTC).isoformat(),
                        "recovered": True,
                    }
                ),
            )
        return {"outcome": "already_applied", "run_id": run_id}
    _verify_seal(run_root, seal)
    rows = _load_rows(run_root / "normalized.jsonl")
    plan = _read_json(run_root / "apply_plan.json")
    envelope = _read_json(run_root / "envelope.json")
    with workspace_lock(root, "import-apply"):
        journal_path = run_root / "apply_journal.json"
        if journal_path.exists():
            journal = _read_json(journal_path)
        else:
            _verify_preconditions(root, seal["preconditions"])
            journal = {
                "schema_version": "1.0.0",
                "kind": "import",
                "run_id": run_id,
                "applied_at": now.astimezone(UTC).isoformat(),
            }
            fault_checkpoint(FaultPoint.IMPORT_JOURNAL_WRITE)
            secure_write(journal_path, canonical_json_bytes(journal), exclusive=True)
        now = datetime.fromisoformat(str(journal["applied_at"]).replace("Z", "+00:00"))
        started = {
            "event": "apply_started",
            "kind": "import",
            "run_id": run_id,
            "at": now.astimezone(UTC).isoformat(),
            "seal": seal["seal_sha256"],
        }
        if not event_exists(root, "apply_started", "import", run_id):
            fault_checkpoint(FaultPoint.IMPORT_APPLY_STARTED)
            secure_append(root / "logs" / "events.jsonl", canonical_json_bytes(started))
        applied_ids: list[str] = []
        for decision in plan["decisions"]:
            record_id = decision["record_id"]
            record_type = record_id.split("_", 1)[0]
            existing_projection = _projection(root, record_id)
            if decision["decision"] in {"reject", "pending"}:
                row = rows.get(record_id)
                source_hash = next(
                    item.get("source_hash")
                    for item in json.loads((run_root / "diff.json").read_text())
                    if item["record_id"] == record_id
                )
                pending = {
                    "source_hash": source_hash,
                    "source_record": row,
                    "snapshot_id": run_id,
                    "drive_version": envelope["drive"]["version"],
                    "status": "rejected" if decision["decision"] == "reject" else "quarantined",
                    "observed_at": now.astimezone(UTC).isoformat(),
                    "reason_codes": [decision["status"]],
                }
                fault_checkpoint(FaultPoint.IMPORT_PROJECTION_WRITE)
                secure_replace(
                    root / "data" / "source_projection" / f"{record_id}.json",
                    canonical_json_bytes(
                        {
                            "record_id": record_id,
                            "applied": None
                            if existing_projection is None
                            else existing_projection.get("applied"),
                            "pending": pending,
                        }
                    ),
                )
                continue
            if decision["decision"] == "unchanged":
                if existing_projection and existing_projection.get("pending") is not None:
                    existing_projection["pending"] = None
                    fault_checkpoint(FaultPoint.IMPORT_PROJECTION_WRITE)
                    secure_replace(
                        root / "data" / "source_projection" / f"{record_id}.json",
                        canonical_json_bytes(existing_projection),
                    )
                continue
            if decision["status"] == "tombstone_pending":
                canonical = _canonical_record(root, record_type, record_id)
                if canonical is None:
                    raise IntakeError(f"missing canonical tombstone target: {record_id}")
                canonical["status"] = "archived"
                canonical["archived_at"] = now.astimezone(UTC).isoformat()
                canonical["updated_at"] = now.astimezone(UTC).isoformat()
                canonical_hash = _hash(canonical_json_bytes(canonical))
                tombstone_hash = _hash(f"tombstone:{record_id}".encode())
                fault_checkpoint(FaultPoint.IMPORT_RECORD_WRITE)
                secure_replace(
                    root / "data" / DOMAIN_DIRECTORIES[record_type] / f"{record_id}.json",
                    canonical_json_bytes(canonical),
                )
                projection = {
                    "record_id": record_id,
                    "applied": {
                        "source_hash": tombstone_hash,
                        "source_record": None,
                        "canonical_hash": canonical_hash,
                        "snapshot_id": run_id,
                        "drive_version": envelope["drive"]["version"],
                        "status": "archived",
                        "applied_at": now.astimezone(UTC).isoformat(),
                        "reason_codes": ["SOURCE_TOMBSTONE_APPROVED"],
                    },
                    "pending": None,
                }
                fault_checkpoint(FaultPoint.IMPORT_PROJECTION_WRITE)
                secure_replace(
                    root / "data" / "source_projection" / f"{record_id}.json",
                    canonical_json_bytes(projection),
                )
                applied_ids.append(record_id)
                continue
            row = rows[record_id]
            canonical = {key: value for key, value in row.items() if key != "quarantined"}
            source_hash = _hash(canonical_json_bytes(row))
            canonical_hash = _hash(canonical_json_bytes(canonical))
            domain = DOMAIN_DIRECTORIES[str(row["type"])]
            fault_checkpoint(FaultPoint.IMPORT_RECORD_WRITE)
            secure_replace(
                root / "data" / domain / f"{record_id}.json", canonical_json_bytes(canonical)
            )
            projection = {
                "record_id": record_id,
                "applied": {
                    "source_hash": source_hash,
                    "source_record": row,
                    "canonical_hash": canonical_hash,
                    "snapshot_id": run_id,
                    "drive_version": envelope["drive"]["version"],
                    "status": "applied",
                    "applied_at": now.astimezone(UTC).isoformat(),
                    "reason_codes": [],
                },
                "pending": None,
            }
            fault_checkpoint(FaultPoint.IMPORT_PROJECTION_WRITE)
            secure_replace(
                root / "data" / "source_projection" / f"{record_id}.json",
                canonical_json_bytes(projection),
            )
            applied_ids.append(record_id)
        snapshot = {
            "snapshot_id": run_id,
            "drive": envelope["drive"],
            "normalized_sha256": envelope["normalized_sha256"],
            "applied_ids": sorted(applied_ids),
        }
        fault_checkpoint(FaultPoint.IMPORT_SNAPSHOT_WRITE)
        secure_write_once(
            root / "data" / "source_snapshots" / f"{run_id}.json",
            canonical_json_bytes(snapshot),
        )
        manifest = load_manifest(root)
        manifest["source"]["last_fully_applied_drive_version"] = envelope["drive"]["version"]
        fault_checkpoint(FaultPoint.IMPORT_MANIFEST_WRITE)
        secure_replace(root / ".opc" / "manifest.json", canonical_json_bytes(manifest))
        result = {"outcome": "applied", "run_id": run_id, "applied_ids": sorted(applied_ids)}
        fault_checkpoint(FaultPoint.IMPORT_RESULT_WRITE)
        secure_write_once(result_path, canonical_json_bytes(result))
        completed = {
            "event": "apply_completed",
            "kind": "import",
            "run_id": run_id,
            "at": now.astimezone(UTC).isoformat(),
        }
        if not event_exists(root, "apply_completed", "import", run_id):
            fault_checkpoint(FaultPoint.IMPORT_APPLY_COMPLETED)
            secure_append(root / "logs" / "events.jsonl", canonical_json_bytes(completed))
    return result
