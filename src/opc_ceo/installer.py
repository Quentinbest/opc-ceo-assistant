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


def _skill_source(project_root: Path, skill_name: str) -> Path:
    source = project_root / "skills" / skill_name
    if not source.is_dir():
        source = Path(__file__).resolve().parent / "bundled_skill" / skill_name
    return source


def _skill_hashes(project_root: Path, skill_names: list[str]) -> dict[str, dict[str, str]]:
    hashes: dict[str, dict[str, str]] = {}
    for skill_name in skill_names:
        source = _skill_source(project_root, skill_name)
        if not source.is_dir():
            raise FileNotFoundError(skill_name)
        hashes[skill_name] = _hashes(source)
    return hashes


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
    include_p1: bool = False,
) -> dict[str, Any]:
    skill_names = ["opc-ceo-office", *([] if not include_p1 else ["opc-sales-pipeline"])]
    try:
        source_hashes = _skill_hashes(project_root, skill_names)
    except FileNotFoundError as error:
        return {
            "outcome": "failed",
            "diagnostics": [{"code": "SKILL_SOURCE_MISSING", "skill": str(error)}],
        }
    state_root = codex_home / "opc-ceo-installs"
    state_path = state_root / "opc-skills.json"
    legacy_state_path = state_root / "opc-ceo-office.json"
    installed_state = json.loads(state_path.read_text()) if state_path.exists() else None
    if installed_state is None and legacy_state_path.exists():
        legacy_state = json.loads(legacy_state_path.read_text())
        installed_state = {"skills": {"opc-ceo-office": legacy_state.get("files", {})}}
    target_hashes = {
        skill_name: _hashes(codex_home / "skills" / skill_name)
        for skill_name in skill_names
        if (codex_home / "skills" / skill_name).exists()
    }
    target_drift = bool(
        installed_state
        and target_hashes
        and target_hashes != installed_state.get("skills")
        and target_hashes != source_hashes
    )
    source_changed = bool(installed_state and source_hashes != installed_state.get("skills"))
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
            "skills": skill_names,
        }
    if target_hashes == source_hashes:
        outcome = "up_to_date"
    else:
        existing_targets = [codex_home / "skills" / skill for skill in skill_names]
        if any(target.exists() for target in existing_targets):
            if not update:
                return {
                    "outcome": "drift_blocked",
                    "diagnostics": [{"code": "INSTALL_UPDATE_REQUIRED"}],
                    "skills": skill_names,
                }
            backup_name = "opc-skills" if include_p1 else "opc-ceo-office"
            backup = codex_home / "backups" / backup_name / datetime.now(UTC).strftime(
                "%Y%m%dT%H%M%SZ"
            )
            backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fault_checkpoint(FaultPoint.INSTALL_BACKUP_CREATED)
            if not backup.exists():
                if include_p1:
                    backup.mkdir(mode=0o700)
                    for target in existing_targets:
                        if target.exists():
                            shutil.copytree(target, backup / target.name)
                else:
                    shutil.copytree(existing_targets[0], backup)
            fault_checkpoint(FaultPoint.INSTALL_TARGET_REMOVED)
            for target in existing_targets:
                if target.exists():
                    shutil.rmtree(target)
            outcome = "updated"
        else:
            outcome = "installed"
        fault_checkpoint(FaultPoint.INSTALL_TARGET_WRITTEN)
        for skill_name in skill_names:
            _copy_tree(_skill_source(project_root, skill_name), codex_home / "skills" / skill_name)
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fault_checkpoint(FaultPoint.INSTALL_MANIFEST_WRITTEN)
    state_path.write_bytes(
        canonical_json_bytes({"version": __version__, "skills": source_hashes})
    )
    state_path.chmod(0o600)
    if install_tool:
        fault_checkpoint(FaultPoint.INSTALL_TOOL_INSTALLED)
        subprocess.run(
            ["uv", "tool", "install", "--from", str(project_root), "--force", "opc-ceo"],
            check=True,
        )
    return {
        "outcome": outcome,
        "diagnostics": [],
        "target": str(codex_home / "skills" / "opc-ceo-office"),
        "skills": skill_names,
    }
