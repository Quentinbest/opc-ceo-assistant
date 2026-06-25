from __future__ import annotations

import json
import os
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import opc_ceo.contracts as contracts
import opc_ceo.installer as installer
import opc_ceo.workspace as workspace
from opc_ceo.diagnostics import (
    cleanup_expired_raw_copies,
    validate_workspace,
    verify_connector_receipt,
    workspace_status,
)


def test_canonical_json_rejects_invalid_keys_duplicates_and_values() -> None:
    with pytest.raises(TypeError, match="keys"):
        contracts.canonical_json_bytes({1: "value"})
    with pytest.raises(ValueError, match="duplicate"):
        contracts.canonical_json_bytes({"é": 1, "e\u0301": 2})
    with pytest.raises(TypeError, match="unsupported"):
        contracts.canonical_json_bytes({"value": object()})
    assert contracts.canonical_json_bytes((True, None, 3)) == b"[true,null,3]\n"


@pytest.mark.parametrize("value", [[], {"version": "2.0.0"}])
def test_load_contract_rejects_unsupported_shapes(tmp_path: Path, value: object) -> None:
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="unsupported"):
        contracts.load_contract(path)


def test_contract_cli_reports_generate_check_drift(tmp_path: Path) -> None:
    assert contracts.main(["generate", "--output", str(tmp_path)]) == 0
    (tmp_path / "schemas" / "1.0.0" / "opc-workspace.schema.json").write_text("{}\n")
    assert contracts.main(["generate", "--check", "--output", str(tmp_path)]) == 1


def test_build_workbook_rejects_missing_active_sheet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class BrokenWorkbook:
        active = None

    monkeypatch.setattr(contracts, "Workbook", BrokenWorkbook)
    with pytest.raises(ValueError, match="no active"):
        contracts.build_workbook(contracts.load_contract(), tmp_path / "broken.xlsx")


def test_zip_normalization_requires_modified_core_property(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("docProps/core.xml", "<root />")
    with pytest.raises(ValueError, match="modified timestamp"):
        contracts._normalize_zip(source, tmp_path / "normalized.xlsx")


def test_secure_replace_cleans_temporary_file_on_replace_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "target.json"

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("opc_ceo.workspace.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        workspace.secure_replace(target, b"{}\n")
    assert list(tmp_path.iterdir()) == []


def test_secure_replace_closes_descriptor_on_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_write = os.write

    def fail_write(descriptor: int, content: bytes) -> int:
        raise OSError("write failed")

    monkeypatch.setattr("opc_ceo.workspace.os.write", fail_write)
    with pytest.raises(OSError, match="write failed"):
        workspace.secure_replace(tmp_path / "target", b"value")
    monkeypatch.setattr("opc_ceo.workspace.os.write", original_write)
    assert list(tmp_path.iterdir()) == []


def test_secure_write_once_and_event_missing_boundaries(tmp_path: Path) -> None:
    target = tmp_path / "immutable.json"
    workspace.secure_write_once(target, b"one")
    workspace.secure_write_once(target, b"one")
    with pytest.raises(workspace.WorkspaceError, match="immutable"):
        workspace.secure_write_once(target, b"two")
    assert workspace.event_exists(tmp_path / "missing", "event", "kind", "run") is False


def test_workspace_lock_conflict_and_invalid_manifest(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace.initialize_workspace(root, approved=True)
    lock = root / ".opc" / "locks" / "test.lock"
    lock.write_text("held")
    with (
        pytest.raises(workspace.WorkspaceError, match="active Workspace lock"),
        workspace.workspace_lock(root, "test"),
    ):
        pass
    lock.unlink()
    (root / ".opc" / "manifest.json").write_text("[]\n")
    with pytest.raises(workspace.WorkspaceError, match="invalid manifest"):
        workspace.load_manifest(root)


def test_workspace_diagnostics_cover_permissions_missing_raw_and_recovery(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert validate_workspace(missing)[0]["code"] == "WORKSPACE_NOT_INITIALIZED"

    root = tmp_path / "workspace"
    workspace.initialize_workspace(root, approved=True)
    manifest_path = root / ".opc" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = "2.0.0"
    manifest_path.write_text(json.dumps(manifest))
    manifest_path.chmod(0o644)
    (root / "data" / "risks").chmod(0o755)
    os.rmdir(root / "data" / "projects")
    raw = root / "inbox" / "imports" / "failed" / "source.xlsx"
    raw.parent.mkdir()
    raw.write_bytes(b"raw")
    events = root / "logs" / "events.jsonl"
    events.write_text('{"event":"apply_started","kind":"import","run_id":"r1"}\n')

    codes = {item["code"] for item in validate_workspace(root)}
    assert {
        "UNSUPPORTED_VERSION",
        "MISSING_DIRECTORY",
        "INSECURE_DIRECTORY_MODE",
        "INSECURE_FILE_MODE",
        "RAW_RETENTION_PENDING",
    } <= codes
    status = workspace_status(root)
    assert status["outcome"] == "recovery_required"
    assert status["pending_recovery"] == ["import:r1"]
    assert workspace_status(missing)["outcome"] == "degraded"


def test_workspace_status_cleans_owned_expired_raw_copy(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace.initialize_workspace(root, approved=True)
    raw = root / "inbox" / "imports" / "failed" / "source.xlsx"
    raw.parent.mkdir()
    raw.write_bytes(b"failed export")
    expired = time.time() - 169 * 3600
    os.utime(raw, (expired, expired))

    status = workspace_status(root)

    assert status["outcome"] == "healthy"
    assert not raw.exists()
    assert status["cleanup"]["deleted_raw_copies"] == 1


def test_workspace_status_redacts_configured_spreadsheet_id(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace.initialize_workspace(root, approved=True)
    manifest_path = root / ".opc" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["source"]["spreadsheet_id"] = "sensitive-id"
    workspace.secure_replace(manifest_path, contracts.canonical_json_bytes(manifest))

    source = workspace_status(root)["source"]

    assert source["spreadsheet_id_configured"] is True
    assert source["spreadsheet_id_hash"].startswith("sha256:")
    assert "sensitive-id" not in str(source)


def test_raw_cleanup_rejects_expired_symlink(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace.initialize_workspace(root, approved=True)
    target = tmp_path / "external.xlsx"
    target.write_bytes(b"external")
    raw = root / "inbox" / "imports" / "failed" / "source.xlsx"
    raw.parent.mkdir()
    raw.symlink_to(target)

    deleted, diagnostics = cleanup_expired_raw_copies(
        root, now=datetime.now(UTC) + timedelta(hours=169)
    )

    assert deleted == 0
    assert diagnostics[0]["code"] == "RAW_RETENTION_CLEANUP_ERROR"
    assert raw.is_symlink()


def test_connector_receipt_rejects_invalid_and_malformed(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{")
    valid, diagnostics = verify_connector_receipt(malformed)
    assert valid is False
    assert "message" in diagnostics[0]

    invalid = tmp_path / "invalid.json"
    invalid.write_text(
        json.dumps(
            {
                "status": "failed",
                "capabilities": {
                    "xlsx_export": {"named_ranges": []},
                    "host_attachment_cleanup": {},
                    "raw_cells_entered_host_context": True,
                    "drive_metadata": {"version": "bad"},
                },
            }
        )
    )
    assert verify_connector_receipt(invalid) == (
        False,
        [{"code": "CONNECTOR_CAPABILITY_ERROR"}],
    )


def test_installer_missing_source_update_backup_and_tool_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing_bundle = tmp_path / "missing-bundle"
    monkeypatch.setattr(installer, "__file__", str(missing_bundle / "installer.py"))
    assert (
        installer.install_skill(tmp_path / "none", tmp_path / "home", install_tool=False)["outcome"]
        == "failed"
    )

    project = tmp_path / "project"
    source = project / "skills" / "opc-ceo-office"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("v1")
    home = tmp_path / "codex"
    assert installer.install_skill(project, home, install_tool=False)["outcome"] == "installed"
    assert installer.install_skill(project, home, install_tool=False)["outcome"] == "up_to_date"
    (source / "SKILL.md").write_text("v2")
    assert installer.install_skill(project, home, install_tool=False)["outcome"] == "drift_blocked"

    calls: list[list[str]] = []
    monkeypatch.setattr(
        "opc_ceo.installer.subprocess.run", lambda command, check: calls.append(command)
    )
    updated = installer.install_skill(project, home, update=True, install_tool=True)
    assert updated["outcome"] == "updated"
    assert calls[0][:3] == ["uv", "tool", "install"]
    assert list((home / "backups" / "opc-ceo-office").glob("*/SKILL.md"))
