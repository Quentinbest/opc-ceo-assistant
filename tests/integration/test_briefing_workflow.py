from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opc_ceo.briefing import (
    BriefingError,
    apply_briefing,
    draft_briefing,
    render_briefing,
)
from opc_ceo.contracts import canonical_json_bytes
from opc_ceo.diagnostics import workspace_status
from opc_ceo.workspace import initialize_workspace, secure_replace

NOW = datetime(2026, 6, 20, 8, 0, tzinfo=UTC)


def workspace_with_records(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    initialize_workspace(root, approved=True)
    records = {
        "priorities/priority_growth.json": {
            "type": "priority",
            "record_id": "priority_growth",
            "title": "Grow recurring revenue",
            "status": "active",
            "updated_at": "2026-06-19T08:00:00+08:00",
            "priority_kind": "objective",
            "weight": 5,
            "target_date": "2026-06-21",
            "amount": "30000.00",
            "currency": "CNY",
        },
        "receivables/receivable_acme.json": {
            "type": "receivable",
            "record_id": "receivable_acme",
            "title": "June milestone",
            "status": "open",
            "updated_at": "2026-06-19T09:00:00+08:00",
            "due_date": "2026-06-18",
            "total_amount": "50000.00",
            "outstanding_amount": "12000.00",
            "currency": "CNY",
            "disputed": False,
        },
        "risks/risk_archived.json": {
            "type": "risk",
            "record_id": "risk_archived",
            "title": "Closed historical risk",
            "status": "archived",
            "updated_at": "2026-06-18T09:00:00+08:00",
            "archived_at": "2026-06-19T09:00:00+08:00",
            "severity": "critical",
            "due_date": "2026-06-17",
        },
    }
    for relative, record in records.items():
        secure_replace(root / "data" / relative, canonical_json_bytes(record))
    manifest_path = root / ".opc" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["source"]["last_successful_refresh_at"] = NOW.isoformat()
    manifest["source"]["last_fully_applied_drive_version"] = "3"
    secure_replace(manifest_path, canonical_json_bytes(manifest))
    return root


def test_briefing_requires_dispositions_and_sealed_apply(tmp_path: Path) -> None:
    root = workspace_with_records(tmp_path)

    drafted = draft_briefing(root, now=NOW, language="zh-CN")
    assert drafted["outcome"] == "drafted"
    assert [item["token"] for item in drafted["top_approval_view"]] == ["T1", "T2"]
    serialized_view = json.dumps(drafted["top_approval_view"], ensure_ascii=False)
    assert "receivable_acme" not in serialized_view
    assert "priority_growth" not in serialized_view
    assert "June milestone" not in serialized_view

    with pytest.raises(BriefingError, match="missing disposition"):
        render_briefing(root, drafted["run_id"], {"T1": {"kind": "act", "next_action": "Collect"}})

    rendered = render_briefing(
        root,
        drafted["run_id"],
        {
            "T1": {"kind": "act", "next_action": "Confirm payment date"},
            "T2": {"kind": "defer", "review_date": "2026-06-22"},
        },
    )
    assert rendered["outcome"] == "sealed"
    assert "今日 CEO 简报" in rendered["brief_markdown"]

    applied = apply_briefing(
        root,
        drafted["run_id"],
        confirm=rendered["approval_token"],
        now=NOW,
    )
    assert applied["outcome"] == "applied"
    assert (root / "briefs" / "daily" / "2026-06-20-r001.md").exists()
    assert len(list((root / "data" / "decisions").glob("decision_*.json"))) == 2
    assert (
        apply_briefing(
            root,
            drafted["run_id"],
            confirm=rendered["approval_token"],
            now=NOW,
        )["outcome"]
        == "already_applied"
    )
    status = workspace_status(root)
    assert status["briefings"]["runs"] == 1
    assert status["briefings"]["states"]["applied"] == 1
    assert status["briefings"]["canonical_revisions"] == 1
    assert status["briefings"]["decisions"] == 2
    assert status["briefings"]["latest"]["state"] == "applied"


def test_stale_source_boundary_is_diagnostic_only_after_168_hours(tmp_path: Path) -> None:
    root = workspace_with_records(tmp_path)

    at_limit = draft_briefing(
        root,
        now=NOW + timedelta(hours=168),
        language="en",
        connector_failed=True,
        allow_stale=True,
    )
    assert at_limit["outcome"] == "stale_drafted"

    too_old = draft_briefing(
        root,
        now=NOW + timedelta(hours=168, seconds=1),
        language="en",
        connector_failed=True,
        allow_stale=True,
    )
    assert too_old["outcome"] == "stale_too_old"
    assert too_old["top_approval_view"] == []
