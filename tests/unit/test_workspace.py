from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from pathlib import Path

import pytest

from opc_ceo.contracts import canonical_json_bytes
from opc_ceo.workspace import (
    WorkspaceError,
    ensure_supported_workspace,
    initialize_workspace,
    resolve_workspace_path,
    secure_replace,
)


def test_initialize_workspace_creates_secure_manifest(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    result = initialize_workspace(root, approved=True)

    assert result == "initialized"
    manifest_path = root / ".opc" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["schema_version"] == "1.0.0"
    assert manifest["workbook_contract_version"] == "1.0.0"
    schema_path = root / ".opc" / "schemas" / "1.0.0" / "opc-workspace.schema.json"
    contract_path = root / ".opc" / "schemas" / "1.0.0" / "workbook-contract.json"
    assert manifest["schema_sha256"] == hashlib.sha256(schema_path.read_bytes()).hexdigest()
    assert (
        manifest["workbook_contract_sha256"]
        == hashlib.sha256(contract_path.read_bytes()).hexdigest()
    )
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert initialize_workspace(root, approved=True) == "already_initialized"


def test_initialize_workspace_requires_approval(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    assert initialize_workspace(root, approved=False) == "declined"
    assert not root.exists()


def test_resolve_workspace_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceError, match="outside"):
        resolve_workspace_path(root, "escape/file.json")


def test_unsupported_workspace_creates_validated_backup_once(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    initialize_workspace(root, approved=True)
    manifest_path = root / ".opc" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = "2.0.0"
    secure_replace(manifest_path, canonical_json_bytes(manifest))

    with pytest.raises(WorkspaceError, match="unsupported Workspace version"):
        ensure_supported_workspace(root)

    backups = list((root / ".opc" / "backups").glob("unsupported-*.zip"))
    assert len(backups) == 1
    with zipfile.ZipFile(backups[0]) as archive:
        assert archive.testzip() is None
        assert ".opc/manifest.json" in archive.namelist()
    with pytest.raises(WorkspaceError, match="unsupported Workspace version"):
        ensure_supported_workspace(root)
    assert list((root / ".opc" / "backups").glob("unsupported-*.zip")) == backups


def test_unsupported_workspace_rejects_corrupt_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    initialize_workspace(root, approved=True)
    manifest_path = root / ".opc" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = "2.0.0"
    secure_replace(manifest_path, canonical_json_bytes(manifest))
    monkeypatch.setattr("opc_ceo.workspace.zipfile.ZipFile.testzip", lambda archive: "bad")

    with pytest.raises(WorkspaceError, match="backup is corrupt"):
        ensure_supported_workspace(root)
