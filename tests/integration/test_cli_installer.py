from __future__ import annotations

import json
from pathlib import Path

from opc_ceo.cli import main
from opc_ceo.installer import install_skill


def invoke(capsys: object, args: list[str]) -> tuple[int, dict[str, object]]:
    code = main(args)
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    return code, json.loads(captured.out)


def test_cli_init_validate_status_and_receipt(tmp_path: Path, capsys: object) -> None:
    root = tmp_path / "workspace"
    code, initialized = invoke(
        capsys, ["--workspace", str(root), "--format", "json", "init", "--approve"]
    )
    assert code == 0
    assert initialized["outcome"] == "initialized"
    assert initialized["wrote_state"] is True

    code, validated = invoke(
        capsys, ["--workspace", str(root), "--format", "json", "validate", "--strict"]
    )
    assert code == 0
    assert validated["outcome"] == "valid"

    code, status = invoke(capsys, ["--workspace", str(root), "--format", "json", "status"])
    assert code == 0
    assert status["outcome"] == "healthy"

    receipt = Path(__file__).parents[2] / "evidence" / "connector-capability-v1.json"
    code, verified = invoke(
        capsys,
        [
            "--workspace",
            str(root),
            "--format",
            "json",
            "connector-receipt",
            "verify",
            "--receipt",
            str(receipt),
        ],
    )
    assert code == 0
    assert verified["outcome"] == "verified"


def test_validate_detects_schema_mirror_drift(tmp_path: Path, capsys: object) -> None:
    root = tmp_path / "workspace"
    invoke(capsys, ["init", "--approve", "--workspace", str(root), "--format", "json"])
    schema = root / ".opc" / "schemas" / "1.0.0" / "opc-workspace.schema.json"
    schema.write_text("{}\n")

    code, drifted = invoke(
        capsys, ["validate", "--strict", "--workspace", str(root), "--format", "json"]
    )
    assert code == 1
    assert drifted["outcome"] == "invalid"
    diagnostics = drifted["diagnostics"]
    assert isinstance(diagnostics, list)
    assert isinstance(diagnostics[0], dict)
    assert diagnostics[0]["code"] == "SCHEMA_MIRROR_DRIFT"


def test_cli_accepts_global_options_after_subcommand(tmp_path: Path, capsys: object) -> None:
    root = tmp_path / "workspace"
    code, initialized = invoke(
        capsys,
        ["init", "--approve", "--workspace", str(root), "--format", "json"],
    )
    assert code == 0
    assert initialized["outcome"] == "initialized"


def test_skill_installer_detects_drift(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[2]
    codex_home = tmp_path / ".codex"

    installed = install_skill(project_root, codex_home, install_tool=False)
    assert installed["outcome"] == "installed"
    assert (
        install_skill(project_root, codex_home, check=True, install_tool=False)["outcome"]
        == "up_to_date"
    )

    target = codex_home / "skills" / "opc-ceo-office" / "SKILL.md"
    target.write_text(target.read_text() + "\nlocal drift\n")
    assert (
        install_skill(project_root, codex_home, check=True, install_tool=False)["outcome"]
        == "drift_blocked"
    )
