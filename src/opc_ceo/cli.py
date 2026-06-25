from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opc_ceo.briefing import apply_briefing, draft_briefing, render_briefing
from opc_ceo.config import load_config
from opc_ceo.contracts import canonical_json_bytes
from opc_ceo.diagnostics import validate_workspace, verify_connector_receipt, workspace_status
from opc_ceo.installer import install_skill
from opc_ceo.intake import apply_import, resolve_import, review_import, stage_import
from opc_ceo.workspace import initialize_workspace


def _envelope(
    command: str,
    outcome: str,
    *,
    run_id: str | None = None,
    wrote_state: bool = False,
    write_scope: str = "none",
    data: object = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "command": command,
        "run_id": run_id,
        "outcome": outcome,
        "wrote_state": wrote_state,
        "write_scope": write_scope,
        "data": {} if data is None else data,
        "diagnostics": diagnostics or [],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opc-workspace")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--format", choices=["human", "json"], default="human")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--approve", action="store_true")
    validate = commands.add_parser("validate")
    validate.add_argument("--strict", action="store_true")
    commands.add_parser("status")
    backup = commands.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)

    imports = commands.add_parser("import").add_subparsers(dest="import_command", required=True)
    stage = imports.add_parser("stage")
    stage.add_argument("--source", type=Path, required=True)
    stage.add_argument("--metadata", type=Path, required=True)
    review = imports.add_parser("review")
    review.add_argument("--run", required=True)
    resolve = imports.add_parser("resolve")
    resolve.add_argument("--run", required=True)
    resolve.add_argument("--resolution", type=Path, required=True)
    apply = imports.add_parser("apply")
    apply.add_argument("--run", required=True)
    apply.add_argument("--confirm", required=True)

    briefings = commands.add_parser("briefing").add_subparsers(
        dest="briefing_command", required=True
    )
    draft = briefings.add_parser("draft")
    draft.add_argument("--language", choices=["zh-CN", "en", "bilingual"], default="zh-CN")
    draft.add_argument("--allow-stale", action="store_true")
    draft.add_argument("--connector-failed", action="store_true", help=argparse.SUPPRESS)
    render = briefings.add_parser("render")
    render.add_argument("--run", required=True)
    render.add_argument("--dispositions", type=Path, required=True)
    brief_apply = briefings.add_parser("apply")
    brief_apply.add_argument("--run", required=True)
    brief_apply.add_argument("--confirm", required=True)

    connector = commands.add_parser("connector-receipt").add_subparsers(
        dest="connector_command", required=True
    )
    verify = connector.add_parser("verify")
    verify.add_argument("--receipt", type=Path, required=True)
    install = commands.add_parser("install")
    install.add_argument("--check", action="store_true")
    install.add_argument("--update", action="store_true")
    return parser


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _hoist_global_options(argv: list[str]) -> list[str]:
    global_options: list[str] = []
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in {"--workspace", "--format"}:
            if index + 1 >= len(argv):
                remaining.append(token)
                index += 1
                continue
            global_options.extend((token, argv[index + 1]))
            index += 2
            continue
        if token.startswith("--workspace=") or token.startswith("--format="):
            global_options.append(token)
        else:
            remaining.append(token)
        index += 1
    return [*global_options, *remaining]


def _execute(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    root = load_config(args.workspace).workspace
    now = datetime.now(UTC)
    if args.command == "init":
        outcome = initialize_workspace(root, approved=args.approve)
        return 0, _envelope(
            "init",
            outcome,
            wrote_state=outcome == "initialized",
            write_scope="staging" if outcome == "initialized" else "none",
        )
    if args.command == "validate":
        diagnostics = validate_workspace(root)
        outcome = "valid" if not diagnostics else "invalid"
        return (0 if not diagnostics else 1), _envelope(
            "validate", outcome, diagnostics=diagnostics
        )
    if args.command == "status":
        status = workspace_status(root)
        return (6 if status["outcome"] == "recovery_required" else 0), _envelope(
            "status", status["outcome"], data=status, diagnostics=status["diagnostics"]
        )
    if args.command == "backup":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        shutil.make_archive(str(args.output), "zip", root)
        return 0, _envelope(
            "backup",
            "created",
            wrote_state=True,
            write_scope="staging",
            data={"path": f"{args.output}.zip"},
        )
    if args.command == "connector-receipt":
        valid, diagnostics = verify_connector_receipt(args.receipt)
        return (0 if valid else 8), _envelope(
            "connector-receipt", "verified" if valid else "invalid", diagnostics=diagnostics
        )
    if args.command == "import":
        if args.import_command == "stage":
            result = stage_import(root, args.source, _load_object(args.metadata), now=now)
            return (0 if result["outcome"] != "blocked" else 1), _envelope(
                "import.stage",
                result["outcome"],
                run_id=result["run_id"],
                wrote_state=result["run_id"] is not None,
                write_scope="staging",
                diagnostics=result["diagnostics"],
            )
        if args.import_command == "review":
            data = review_import(root, args.run)
            return 0, _envelope("import.review", "review_ready", run_id=args.run, data=data)
        if args.import_command == "resolve":
            result = resolve_import(root, args.run, _load_object(args.resolution))
            return 0, _envelope(
                "import.resolve",
                result["outcome"],
                run_id=args.run,
                wrote_state=True,
                write_scope="staging",
                data={"approval_token": result["approval_token"], "review": result["review"]},
            )
        result = apply_import(root, args.run, confirm=args.confirm, now=now)
        return 0, _envelope(
            "import.apply",
            result["outcome"],
            run_id=args.run,
            wrote_state=result["outcome"] == "applied",
            write_scope="canonical",
            data=result,
        )
    if args.command == "briefing":
        if args.briefing_command == "draft":
            result = draft_briefing(
                root,
                now=now,
                language=args.language,
                connector_failed=args.connector_failed,
                allow_stale=args.allow_stale,
            )
            return 0, _envelope(
                "briefing.draft",
                result["outcome"],
                run_id=result["run_id"],
                wrote_state=result["run_id"] is not None,
                write_scope="staging",
                data={"top_approval_view": result["top_approval_view"]},
                diagnostics=result["diagnostics"],
            )
        if args.briefing_command == "render":
            result = render_briefing(root, args.run, _load_object(args.dispositions))
            return 0, _envelope(
                "briefing.render",
                result["outcome"],
                run_id=args.run,
                wrote_state=True,
                write_scope="staging",
                data={
                    "approval_token": result["approval_token"],
                    "brief_markdown": result["brief_markdown"],
                },
            )
        result = apply_briefing(root, args.run, confirm=args.confirm, now=now)
        return 0, _envelope(
            "briefing.apply",
            result["outcome"],
            run_id=args.run,
            wrote_state=result["outcome"] == "applied",
            write_scope="canonical",
            data=result,
        )
    if args.command == "install":
        project_root = Path(__file__).resolve().parents[2]
        codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
        result = install_skill(
            project_root,
            codex_home,
            check=args.check,
            update=args.update,
            install_tool=not bool(os.environ.get("OPC_SKIP_UV_TOOL_INSTALL")),
        )
        return (0 if result["outcome"] in {"installed", "up_to_date", "updated"} else 1), _envelope(
            "install",
            result["outcome"],
            wrote_state=result["outcome"] in {"installed", "updated"},
            write_scope="installation",
            data=result,
            diagnostics=result.get("diagnostics"),
        )
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    raw_argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(_hoist_global_options(raw_argv))
    try:
        code, result = _execute(args)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        code = 1
        result = _envelope(
            str(getattr(args, "command", "unknown")),
            "invalid",
            diagnostics=[
                {
                    "code": type(error).__name__,
                    "message": str(error),
                    "recovery": "Correct the input or run opc-workspace status.",
                }
            ],
        )
    if args.format == "json":
        sys.stdout.buffer.write(canonical_json_bytes(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":  # pragma: no cover - exercised by the installed entry point
    raise SystemExit(main())
