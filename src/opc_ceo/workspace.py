from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any

from opc_ceo.contracts import CONTRACT_VERSION, RESOURCE_ROOT, canonical_json_bytes


class WorkspaceError(ValueError):
    pass


class FaultInjected(RuntimeError):
    pass


class FaultPoint(StrEnum):
    IMPORT_JOURNAL_WRITE = "import.journal_write"
    IMPORT_APPLY_STARTED = "import.apply_started"
    IMPORT_RECORD_WRITE = "import.record_write"
    IMPORT_PROJECTION_WRITE = "import.projection_write"
    IMPORT_SNAPSHOT_WRITE = "import.snapshot_write"
    IMPORT_MANIFEST_WRITE = "import.manifest_write"
    IMPORT_RESULT_WRITE = "import.result_write"
    IMPORT_APPLY_COMPLETED = "import.apply_completed"
    BRIEF_JOURNAL_WRITE = "brief.journal_write"
    BRIEF_APPLY_STARTED = "brief.apply_started"
    BRIEF_REVISION_WRITE = "brief.revision_write"
    BRIEF_MARKDOWN_WRITE = "brief.markdown_write"
    BRIEF_DECISION_WRITE = "brief.decision_write"
    BRIEF_POINTER_WRITE = "brief.pointer_write"
    BRIEF_RESULT_WRITE = "brief.result_write"
    BRIEF_APPLY_COMPLETED = "brief.apply_completed"
    INSTALL_BACKUP_CREATED = "install.backup_created"
    INSTALL_TARGET_REMOVED = "install.target_removed"
    INSTALL_TARGET_WRITTEN = "install.target_written"
    INSTALL_MANIFEST_WRITTEN = "install.manifest_written"
    INSTALL_TOOL_INSTALLED = "install.tool_installed"


def fault_checkpoint(point: FaultPoint) -> None:
    if os.environ.get("OPC_FAULT_POINT") == point.value:
        raise FaultInjected(f"fault injected at {point.value}")


def write_boundary_inventory() -> list[dict[str, str]]:
    inventory: list[dict[str, str]] = []
    for point in FaultPoint:
        owner, purpose = point.value.split(".", 1)
        inventory.append({"fault_point": point.value, "owner": owner, "purpose": purpose})
    return inventory


DIRECTORIES = (
    ".opc/schemas/1.0.0",
    ".opc/locks",
    ".opc/backups",
    "inbox/imports",
    "inbox/briefings",
    "data/priorities",
    "data/pipeline",
    "data/receivables",
    "data/contracts",
    "data/projects",
    "data/risks",
    "data/source_snapshots",
    "data/source_projection",
    "data/decisions",
    "data/briefs",
    "briefs/daily",
    "logs/diagnostics",
)


def resolve_workspace_path(root: Path, relative: str | Path) -> Path:
    root_resolved = root.expanduser().resolve()
    candidate = (root_resolved / relative).resolve(strict=False)
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise WorkspaceError(f"path resolves outside Workspace: {relative}")
    return candidate


def secure_mkdir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def secure_write(path: Path, content: bytes, *, exclusive: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    path.chmod(0o600)


def secure_write_once(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise WorkspaceError(f"immutable file content mismatch: {path.name}")
        return
    secure_write(path, content, exclusive=True)


def event_exists(root: Path, event: str, kind: str, run_id: str) -> bool:
    path = resolve_workspace_path(root, "logs/events.jsonl")
    if not path.exists():
        return False
    return any(
        item.get("event") == event and item.get("kind") == kind and item.get("run_id") == run_id
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and isinstance((item := json.loads(line)), dict)
    )


def secure_replace(path: Path, content: bytes) -> None:
    secure_mkdir(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def secure_unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


def secure_append(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def workspace_lock(root: Path, name: str) -> Iterator[None]:
    path = resolve_workspace_path(root, f".opc/locks/{name}.lock")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise WorkspaceError(f"active Workspace lock: {name}") from error
    try:
        os.write(descriptor, f"{os.getpid()}\n".encode())
        os.fsync(descriptor)
        yield
    finally:
        os.close(descriptor)
        secure_unlink(path)


def default_manifest() -> dict[str, Any]:
    schema = RESOURCE_ROOT / "schemas" / CONTRACT_VERSION / "opc-workspace.schema.json"
    contract = RESOURCE_ROOT / "contracts" / CONTRACT_VERSION / "workbook-contract.json"
    return {
        "schema_version": CONTRACT_VERSION,
        "workbook_contract_version": CONTRACT_VERSION,
        "schema_sha256": hashlib.sha256(schema.read_bytes()).hexdigest(),
        "workbook_contract_sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
        "source": {
            "kind": "google_sheets",
            "spreadsheet_id": None,
            "last_observed_drive_version": None,
            "last_observed_modified_time": None,
            "last_successful_refresh_at": None,
            "last_fully_applied_drive_version": None,
        },
        "approval_amount_thresholds": {"CNY": "10000.00", "USD": "1500.00"},
        "limits": {
            "records": 1000,
            "triage_display_limit": 50,
            "raw_failure_retention_hours": 168,
            "max_stale_hours": 168,
        },
        "host_view": {"mode": "opaque", "bounded_text_consent": False},
    }


def initialize_workspace(root: Path, *, approved: bool) -> str:
    root = root.expanduser()
    manifest_path = root / ".opc" / "manifest.json"
    if manifest_path.exists():
        return "already_initialized"
    if not approved:
        return "declined"
    secure_mkdir(root)
    for relative in DIRECTORIES:
        secure_mkdir(resolve_workspace_path(root, relative))
    schema_source = RESOURCE_ROOT / "schemas" / CONTRACT_VERSION / "opc-workspace.schema.json"
    contract_source = RESOURCE_ROOT / "contracts" / CONTRACT_VERSION / "workbook-contract.json"
    secure_write(
        root / ".opc" / "schemas" / CONTRACT_VERSION / "opc-workspace.schema.json",
        schema_source.read_bytes(),
        exclusive=True,
    )
    secure_write(
        root / ".opc" / "schemas" / CONTRACT_VERSION / "workbook-contract.json",
        contract_source.read_bytes(),
        exclusive=True,
    )
    secure_write(manifest_path, canonical_json_bytes(default_manifest()), exclusive=True)
    secure_write(
        root / ".opc" / "write-boundaries.json",
        canonical_json_bytes(write_boundary_inventory()),
        exclusive=True,
    )
    secure_write(root / "logs" / "events.jsonl", b"", exclusive=True)
    return "initialized"


def load_manifest(root: Path) -> dict[str, Any]:
    path = resolve_workspace_path(root, ".opc/manifest.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise WorkspaceError("invalid manifest")
    return value


def ensure_supported_workspace(root: Path) -> None:
    manifest = load_manifest(root)
    versions = {
        str(manifest.get("schema_version")),
        str(manifest.get("workbook_contract_version")),
    }
    if versions == {CONTRACT_VERSION}:
        return
    fingerprint = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()[:12]
    destination = resolve_workspace_path(root, f".opc/backups/unsupported-{fingerprint}.zip")
    if not destination.exists():
        with tempfile.TemporaryDirectory(prefix="opc-version-backup-") as temporary:
            archive_path = Path(
                shutil.make_archive(str(Path(temporary) / "workspace"), "zip", root_dir=root)
            )
            with zipfile.ZipFile(archive_path) as archive:
                corrupted = archive.testzip()
                if corrupted is not None:
                    raise WorkspaceError(f"incompatible-version backup is corrupt: {corrupted}")
                archive.getinfo(".opc/manifest.json")
            secure_replace(destination, archive_path.read_bytes())
    raise WorkspaceError(
        f"unsupported Workspace version; validated backup created at {destination}"
    )
