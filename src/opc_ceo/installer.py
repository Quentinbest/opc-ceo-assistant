from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opc_ceo import __version__
from opc_ceo.contracts import canonical_json_bytes
from opc_ceo.workspace import FaultPoint, fault_checkpoint


def _hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _copy_tree(source: Path, target: Path) -> None:
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    for source_path in sorted(source.rglob("*")):
        relative = source_path.relative_to(source)
        target_path = target / relative
        if source_path.is_dir():
            target_path.mkdir(mode=0o700, parents=True, exist_ok=True)
            target_path.chmod(0o700)
        else:
            target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                content = source_path.read_bytes()
                os.write(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            target_path.chmod(0o600)


def install_skill(
    project_root: Path,
    codex_home: Path,
    *,
    check: bool = False,
    update: bool = False,
    install_tool: bool = True,
) -> dict[str, Any]:
    source = project_root / "skills" / "opc-ceo-office"
    if not source.is_dir():
        source = Path(__file__).resolve().parent / "bundled_skill" / "opc-ceo-office"
    if not source.is_dir():
        return {"outcome": "failed", "diagnostics": [{"code": "SKILL_SOURCE_MISSING"}]}
    target = codex_home / "skills" / "opc-ceo-office"
    state_root = codex_home / "opc-ceo-installs"
    state_path = state_root / "opc-ceo-office.json"
    source_hashes = _hashes(source)
    installed_state = json.loads(state_path.read_text()) if state_path.exists() else None
    target_hashes = _hashes(target) if target.exists() else None
    target_drift = bool(
        installed_state
        and target.exists()
        and target_hashes != installed_state.get("files")
        and target_hashes != source_hashes
    )
    source_changed = bool(installed_state and source_hashes != installed_state.get("files"))
    if target_drift:
        return {"outcome": "drift_blocked", "diagnostics": [{"code": "INSTALL_DRIFT_ERROR"}]}
    if check:
        outcome = (
            "up_to_date"
            if target_hashes == source_hashes and not source_changed
            else "drift_blocked"
        )
        return {
            "outcome": outcome,
            "diagnostics": [] if outcome == "up_to_date" else [{"code": "INSTALL_DRIFT_ERROR"}],
        }
    if target_hashes == source_hashes:
        outcome = "up_to_date"
    else:
        if target.exists():
            if not update:
                return {
                    "outcome": "drift_blocked",
                    "diagnostics": [{"code": "INSTALL_UPDATE_REQUIRED"}],
                }
            backup = (
                codex_home
                / "backups"
                / "opc-ceo-office"
                / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            )
            backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fault_checkpoint(FaultPoint.INSTALL_BACKUP_CREATED)
            if not backup.exists():
                shutil.copytree(target, backup)
            fault_checkpoint(FaultPoint.INSTALL_TARGET_REMOVED)
            shutil.rmtree(target)
            outcome = "updated"
        else:
            outcome = "installed"
        fault_checkpoint(FaultPoint.INSTALL_TARGET_WRITTEN)
        _copy_tree(source, target)
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fault_checkpoint(FaultPoint.INSTALL_MANIFEST_WRITTEN)
    state_path.write_bytes(canonical_json_bytes({"version": __version__, "files": source_hashes}))
    state_path.chmod(0o600)
    if install_tool:
        fault_checkpoint(FaultPoint.INSTALL_TOOL_INSTALLED)
        subprocess.run(
            ["uv", "tool", "install", "--from", str(project_root), "--force", "opc-ceo"],
            check=True,
        )
    return {"outcome": outcome, "diagnostics": [], "target": str(target)}
