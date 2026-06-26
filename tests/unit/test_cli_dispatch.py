from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

import opc_ceo.cli as cli
from opc_ceo.workspace import initialize_workspace


def parsed(arguments: list[str]) -> argparse.Namespace:
    return cli._parser().parse_args(cli._hoist_global_options(arguments))


def test_load_object_and_global_option_edge_cases(tmp_path: Path) -> None:
    array = tmp_path / "array.json"
    array.write_text("[]\n")
    with pytest.raises(ValueError, match="expected JSON object"):
        cli._load_object(array)
    assert cli._hoist_global_options(["init", "--format"]) == ["init", "--format"]
    assert cli._hoist_global_options(["init", "--format=json", "--workspace=/tmp/w"]) == [
        "--format=json",
        "--workspace=/tmp/w",
        "init",
    ]


def test_execute_backup_connector_and_status_recovery(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    initialize_workspace(root, approved=True)
    backup = tmp_path / "backups" / "opc"
    code, result = cli._execute(
        parsed(["backup", "--output", str(backup), "--workspace", str(root)])
    )
    assert code == 0 and result["outcome"] == "created"
    assert Path(f"{backup}.zip").is_file()

    invalid_receipt = tmp_path / "receipt.json"
    invalid_receipt.write_text("{}\n")
    code, result = cli._execute(
        parsed(
            [
                "connector-receipt",
                "verify",
                "--receipt",
                str(invalid_receipt),
                "--workspace",
                str(root),
            ]
        )
    )
    assert code == 8 and result["outcome"] == "invalid"

    (root / "logs" / "events.jsonl").write_text(
        '{"event":"apply_started","kind":"briefing","run_id":"r1"}\n'
    )
    code, result = cli._execute(parsed(["status", "--workspace", str(root)]))
    assert code == 6 and result["outcome"] == "recovery_required"


def test_execute_import_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    metadata = tmp_path / "metadata.json"
    metadata.write_text("{}\n")
    resolution = tmp_path / "resolution.json"
    resolution.write_text("{}\n")
    root_args = ["--workspace", str(tmp_path / "workspace")]

    monkeypatch.setattr(
        cli,
        "stage_import",
        lambda *args, **kwargs: {
            "outcome": "blocked",
            "run_id": None,
            "diagnostics": [{"code": "blocked"}],
        },
    )
    code, result = cli._execute(
        parsed(
            [
                "import",
                "stage",
                "--source",
                str(tmp_path / "source.xlsx"),
                "--metadata",
                str(metadata),
                *root_args,
            ]
        )
    )
    assert code == 1 and result["outcome"] == "blocked"

    monkeypatch.setattr(cli, "review_import", lambda *args: {"batch_safe": []})
    code, result = cli._execute(parsed(["import", "review", "--run", "r1", *root_args]))
    assert code == 0 and result["outcome"] == "review_ready"

    monkeypatch.setattr(
        cli,
        "resolve_import",
        lambda *args: {
            "outcome": "sealed",
            "approval_token": "r1:seal",
            "review": {},
        },
    )
    code, result = cli._execute(
        parsed(
            [
                "import",
                "resolve",
                "--run",
                "r1",
                "--resolution",
                str(resolution),
                *root_args,
            ]
        )
    )
    assert code == 0 and result["outcome"] == "sealed"

    monkeypatch.setattr(cli, "apply_import", lambda *args, **kwargs: {"outcome": "already_applied"})
    code, result = cli._execute(
        parsed(["import", "apply", "--run", "r1", "--confirm", "r1:seal", *root_args])
    )
    assert code == 0 and result["wrote_state"] is False


def test_execute_briefing_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dispositions = tmp_path / "dispositions.json"
    dispositions.write_text("{}\n")
    root_args = ["--workspace", str(tmp_path / "workspace")]
    monkeypatch.setattr(
        cli,
        "draft_briefing",
        lambda *args, **kwargs: {
            "outcome": "no_candidates",
            "run_id": "b1",
            "top_approval_view": [],
            "diagnostics": [],
        },
    )
    code, result = cli._execute(parsed(["briefing", "draft", *root_args]))
    assert code == 0 and result["outcome"] == "no_candidates"

    monkeypatch.setattr(
        cli,
        "render_briefing",
        lambda *args: {
            "outcome": "sealed",
            "approval_token": "b1:seal",
            "brief_markdown": "brief\n",
        },
    )
    code, result = cli._execute(
        parsed(
            [
                "briefing",
                "render",
                "--run",
                "b1",
                "--dispositions",
                str(dispositions),
                *root_args,
            ]
        )
    )
    assert code == 0 and result["outcome"] == "sealed"

    monkeypatch.setattr(
        cli, "apply_briefing", lambda *args, **kwargs: {"outcome": "already_applied"}
    )
    code, result = cli._execute(
        parsed(["briefing", "apply", "--run", "b1", "--confirm", "b1:seal", *root_args])
    )
    assert code == 0 and result["wrote_state"] is False


@pytest.mark.parametrize("outcome", ["installed", "updated", "drift_blocked"])
def test_execute_install_outcomes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("OPC_SKIP_UV_TOOL_INSTALL", "1")
    monkeypatch.setattr(
        cli,
        "install_skill",
        lambda *args, **kwargs: {"outcome": outcome, "diagnostics": []},
    )
    code, result = cli._execute(parsed(["install", "--workspace", str(tmp_path / "w")]))
    assert code == (1 if outcome == "drift_blocked" else 0)
    assert result["wrote_state"] is (outcome in {"installed", "updated"})


def test_execute_install_can_include_p1_skills(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: dict[str, object] = {}

    def fake_install_skill(*args: object, **kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {"outcome": "installed", "diagnostics": [], "skills": ["opc-ceo-office"]}

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("OPC_SKIP_UV_TOOL_INSTALL", "1")
    monkeypatch.setattr(cli, "install_skill", fake_install_skill)

    code, result = cli._execute(
        parsed(["install", "--include-p1", "--workspace", str(tmp_path / "w")])
    )

    assert code == 0
    assert result["outcome"] == "installed"
    assert observed["include_p1"] is True


def test_execute_rejects_unknown_command() -> None:
    with pytest.raises(AssertionError, match="unreachable"):
        cli._execute(argparse.Namespace(command="unknown", workspace=None))


def test_main_human_output_exception_and_sys_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "workspace"
    assert cli.main(["init", "--approve", "--workspace", str(root)]) == 0
    assert '"outcome": "initialized"' in capsys.readouterr().out

    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]\n")
    assert (
        cli.main(
            [
                "import",
                "stage",
                "--source",
                str(tmp_path / "source.xlsx"),
                "--metadata",
                str(invalid),
                "--workspace",
                str(root),
            ]
        )
        == 1
    )
    assert "ValueError" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["opc-workspace", "validate", "--workspace", str(root)])
    assert cli.main() == 0
    assert "valid" in capsys.readouterr().out
