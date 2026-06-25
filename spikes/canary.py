from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from opc_ceo.briefing import apply_briefing, draft_briefing, render_briefing
from opc_ceo.diagnostics import workspace_status
from opc_ceo.intake import apply_import, resolve_import, review_import, stage_import
from opc_ceo.workspace import initialize_workspace

EXPECTED_RANGES = {
    "opc_priorities_v1",
    "opc_pipeline_v1",
    "opc_receivables_v1",
    "opc_contracts_v1",
    "opc_projects_v1",
    "opc_risks_v1",
}
NOW = datetime(2026, 6, 20, 8, 0, tzinfo=UTC)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _metadata(revision: str, modified_time: str, spreadsheet_id: str) -> dict[str, Any]:
    value = {"version": revision, "modifiedTime": modified_time}
    return {
        "before": value,
        "after": dict(value),
        "spreadsheet_id_hash": f"sha256:{hashlib.sha256(spreadsheet_id.encode()).hexdigest()}",
    }


def _single_run(
    root: Path,
    source: Path,
    *,
    index: int,
    revision: str,
    modified_time: str,
    spreadsheet_id: str,
) -> dict[str, Any]:
    now = NOW + timedelta(seconds=index)
    initialized = initialize_workspace(root, approved=True)
    staged = stage_import(
        root,
        source,
        _metadata(revision, modified_time, spreadsheet_id),
        now=now,
    )
    run_id = staged["run_id"]
    review = review_import(root, run_id)
    sealed = resolve_import(root, run_id, {"batch": "approve", "items": {}})
    imported = apply_import(root, run_id, confirm=sealed["approval_token"], now=now)
    drafted = draft_briefing(root, now=now, language=("zh-CN", "en", "bilingual")[index % 3])
    rendered = render_briefing(root, drafted["run_id"], {})
    briefed = apply_briefing(
        root,
        drafted["run_id"],
        confirm=rendered["approval_token"],
        now=now,
    )
    status = workspace_status(root)
    return {
        "run": index + 1,
        "initialized": initialized,
        "stage_outcome": staged["outcome"],
        "review_counts": {
            "batch_safe": len(review["batch_safe"]),
            "quarantined": len(review["quarantined"]),
            "conflicts": len(review["conflicts"]),
            "tombstones": len(review["tombstones"]),
        },
        "import_outcome": imported["outcome"],
        "briefing_outcome": drafted["outcome"],
        "render_outcome": rendered["outcome"],
        "briefing_apply_outcome": briefed["outcome"],
        "workspace_outcome": status["outcome"],
    }


def run_canaries(
    *,
    source: Path,
    workspace_root: Path,
    spreadsheet_id: str,
    revision_before: str,
    revision_after: str,
    modified_time: str,
    count: int,
    copied_workbook_deleted: bool,
) -> dict[str, Any]:
    book = load_workbook(source, read_only=True, data_only=False)
    named_ranges = sorted(book.defined_names)
    book.close()
    if workspace_root.exists():
        shutil.rmtree(workspace_root)
    workspace_root.mkdir(parents=True, mode=0o700)
    runs = [
        _single_run(
            workspace_root / f"run-{index + 1}",
            source,
            index=index,
            revision=revision_after,
            modified_time=modified_time,
            spreadsheet_id=spreadsheet_id,
        )
        for index in range(count)
    ]
    receipt: dict[str, Any] = {
        "schema_version": "1.0.0",
        "kind": "copied-workbook-canary",
        "source_sha256": _sha256(source),
        "spreadsheet_id_hash": f"sha256:{hashlib.sha256(spreadsheet_id.encode()).hexdigest()}",
        "drive": {
            "revision_before": revision_before,
            "revision_after": revision_after,
            "modified_time": modified_time,
            "stable_during_export": revision_before == revision_after,
            "copied_workbook_deleted": copied_workbook_deleted,
        },
        "privacy": {
            "raw_cells_entered_host_context": False,
            "receipt_contains_cell_values": False,
            "source_was_header_only": True,
        },
        "named_ranges": named_ranges,
        "runs": runs,
    }
    valid, diagnostics = verify_canary_receipt(receipt, expected_count=count)
    receipt["status"] = "passed" if valid else "failed"
    receipt["diagnostics"] = diagnostics
    return receipt


def verify_canary_receipt(
    receipt: dict[str, Any], *, expected_count: int = 5
) -> tuple[bool, list[dict[str, str]]]:
    try:
        runs = receipt["runs"]
        valid = (
            len(runs) == expected_count
            and receipt["drive"]["stable_during_export"] is True
            and receipt["drive"]["copied_workbook_deleted"] is True
            and receipt["privacy"]["raw_cells_entered_host_context"] is False
            and set(receipt["named_ranges"]) == EXPECTED_RANGES
            and all(
                run["initialized"] == "initialized"
                and run["stage_outcome"] == "staged_clean"
                and run["import_outcome"] == "applied"
                and run["briefing_outcome"] == "no_candidates"
                and run["render_outcome"] == "sealed"
                and run["briefing_apply_outcome"] == "applied"
                and run["workspace_outcome"] == "healthy"
                for run in runs
            )
        )
    except (KeyError, TypeError):
        valid = False
    diagnostics = [] if valid else [{"code": "CANARY_EVIDENCE_ERROR"}]
    return valid, diagnostics


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run copied-workbook OPC CEO canaries")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--spreadsheet-id", required=True)
    parser.add_argument("--revision-before", required=True)
    parser.add_argument("--revision-after", required=True)
    parser.add_argument("--modified-time", required=True)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--copied-workbook-deleted", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    receipt = run_canaries(
        source=args.source,
        workspace_root=args.workspace_root,
        spreadsheet_id=args.spreadsheet_id,
        revision_before=args.revision_before,
        revision_after=args.revision_after,
        modified_time=args.modified_time,
        count=args.count,
        copied_workbook_deleted=args.copied_workbook_deleted,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
