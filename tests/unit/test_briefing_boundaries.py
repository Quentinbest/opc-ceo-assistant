from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

import opc_ceo.briefing as briefing
from opc_ceo.contracts import canonical_json_bytes
from opc_ceo.workspace import initialize_workspace, secure_replace

NOW = datetime(2026, 6, 20, 8, 0, tzinfo=UTC)


def ready_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    initialize_workspace(root, approved=True)
    manifest_path = root / ".opc" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["source"]["last_successful_refresh_at"] = NOW.isoformat()
    manifest["source"]["last_fully_applied_drive_version"] = "1"
    secure_replace(manifest_path, canonical_json_bytes(manifest))
    return root


def record(record_type: str, record_id: str, **values: object) -> dict[str, object]:
    return {
        "type": record_type,
        "record_id": record_id,
        "title": record_id,
        "status": "active",
        "updated_at": "2026-06-19T08:00:00+00:00",
        **values,
    }


def test_read_json_and_terminal_record_filtering(tmp_path: Path) -> None:
    root = ready_workspace(tmp_path)
    bad = root / "data" / "risks" / "bad.json"
    bad.write_text("[]\n")
    with pytest.raises(briefing.BriefingError, match="expected object"):
        briefing._read_json(bad)
    bad.unlink()

    terminal = [
        ("pipeline", "pipeline_won", {"status": "won"}),
        ("pipeline", "pipeline_lost", {"status": "lost"}),
        ("receivable", "receivable_paid", {"outstanding_amount": "0"}),
        ("project", "project_done", {"status": "done"}),
        ("project", "project_completed", {"status": "completed"}),
        ("risk", "risk_closed", {"status": "closed"}),
        ("risk", "risk_resolved", {"status": "resolved"}),
        ("contract", "contract_expired", {"status": "expired"}),
        ("contract", "contract_terminated", {"status": "terminated"}),
    ]
    directories = briefing.DOMAIN_DIRECTORIES
    for kind, record_id, values in terminal:
        path = (
            root / "data" / ("pipeline" if kind == "pipeline" else f"{kind}s") / f"{record_id}.json"
        )
        secure_replace(path, canonical_json_bytes(record(kind, record_id, **values)))
    active = record("risk", "risk_active", severity="low")
    secure_replace(root / "data" / "risks" / "risk_active.json", canonical_json_bytes(active))
    assert [item["record_id"] for item in briefing._load_records(root)] == ["risk_active"]
    assert directories


@pytest.mark.parametrize(
    ("item", "expected_due", "expected_severity"),
    [
        (record("priority", "priority_one", target_date="2026-06-21"), "2026-06-21", None),
        (
            record("pipeline", "pipeline_one", next_action_due="2026-06-22"),
            "2026-06-22",
            None,
        ),
        (
            record(
                "receivable",
                "receivable_critical",
                outstanding_amount="1",
                due_date="2026-05-01",
            ),
            "2026-05-01",
            "critical",
        ),
        (
            record(
                "receivable",
                "receivable_high",
                outstanding_amount="1",
                due_date="2026-06-10",
            ),
            "2026-06-10",
            "high",
        ),
        (
            record(
                "contract",
                "contract_one",
                review_date="2026-06-25",
                signature_date="2026-06-23",
                risk_level="high",
            ),
            "2026-06-23",
            "high",
        ),
        (
            record("contract", "contract_none", risk_level="unknown"),
            None,
            None,
        ),
        (
            record("project", "project_healthy", health="healthy"),
            None,
            None,
        ),
        (
            record(
                "project",
                "project_blocked",
                milestone_due="2026-06-24",
                health="critical",
            ),
            "2026-06-24",
            "critical",
        ),
        (
            record(
                "risk",
                "risk_one",
                due_date="2026-06-26",
                mitigation_date="2026-06-22",
                severity="medium",
            ),
            "2026-06-22",
            "medium",
        ),
    ],
)
def test_due_and_severity_by_domain(
    item: dict[str, object], expected_due: str | None, expected_severity: str | None
) -> None:
    due, severity = briefing._due_and_severity(item, NOW.date())
    assert (due.isoformat() if due else None, severity) == (expected_due, expected_severity)


@pytest.mark.parametrize(
    ("due", "bucket"),
    [
        (None, None),
        ("2026-06-19", "overdue"),
        ("2026-06-20", "today"),
        ("2026-06-23", "1_3_days"),
        ("2026-06-27", "4_7_days"),
        ("2026-06-28", None),
    ],
)
def test_due_buckets(due: str | None, bucket: str | None) -> None:
    parsed = date.fromisoformat(due) if due else None
    assert briefing._due_bucket(parsed, NOW.date()) == bucket


def test_candidate_scoring_change_flags_and_threshold_errors() -> None:
    item = record(
        "project",
        "project_one",
        health="high",
        milestone_due="2026-06-20",
        blocked=True,
        strategic_weight=2,
    )
    candidate = briefing._candidate(item, today=NOW.date(), thresholds={}, last_evidence={})
    assert candidate is not None
    assert candidate.mandatory is True
    assert {"BLOCKED", "DUE_TODAY", "SEVERITY_HIGH", "DELTA_NEW"} <= set(candidate.reason_codes)

    source_hash = briefing._hash(canonical_json_bytes(item))
    unchanged = briefing._candidate(
        record("risk", "risk_quiet"),
        today=NOW.date(),
        thresholds={},
        last_evidence={
            "risk_quiet": briefing._hash(canonical_json_bytes(record("risk", "risk_quiet")))
        },
    )
    assert unchanged is None
    changed = briefing._candidate(
        item,
        today=NOW.date(),
        thresholds={},
        last_evidence={"project_one": "different"},
    )
    assert changed is not None and changed.delta == "changed"
    assert source_hash

    future_receivable = record(
        "receivable",
        "receivable_future",
        outstanding_amount="50",
        due_date="2026-06-21",
        disputed=True,
        currency="CNY",
    )
    future = briefing._candidate(
        future_receivable,
        today=NOW.date(),
        thresholds={"CNY": "100"},
        last_evidence={},
    )
    assert future is not None
    assert future.severity is None
    assert "DISPUTED" in future.reason_codes
    assert "AMOUNT_AT_OR_ABOVE_THRESHOLD" not in future.reason_codes

    with pytest.raises(briefing.BriefingError, match="threshold"):
        briefing._candidate(
            record("priority", "priority_money", amount="10", currency="EUR"),
            today=NOW.date(),
            thresholds={},
            last_evidence={},
        )


def test_draft_validation_stale_limit_and_overflow(tmp_path: Path) -> None:
    root = ready_workspace(tmp_path)
    with pytest.raises(briefing.BriefingError, match="language"):
        briefing.draft_briefing(root, now=NOW, language="fr")
    manifest_path = root / ".opc" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["source"]["last_successful_refresh_at"] = None
    secure_replace(manifest_path, canonical_json_bytes(manifest))
    with pytest.raises(briefing.BriefingError, match="no successfully"):
        briefing.draft_briefing(root, now=NOW, language="en")

    manifest["source"]["last_successful_refresh_at"] = NOW.isoformat()
    manifest["limits"]["records"] = 0
    secure_replace(manifest_path, canonical_json_bytes(manifest))
    secure_replace(
        root / "data" / "risks" / "risk_one.json",
        canonical_json_bytes(record("risk", "risk_one", severity="high")),
    )
    with pytest.raises(briefing.BriefingError, match="record limit"):
        briefing.draft_briefing(root, now=NOW, language="en")

    manifest["limits"]["records"] = 10
    manifest["limits"]["triage_display_limit"] = 0
    secure_replace(manifest_path, canonical_json_bytes(manifest))
    overflow = briefing.draft_briefing(root, now=NOW, language="en")
    assert overflow["outcome"] == "triage_only"
    with pytest.raises(briefing.BriefingError, match="explicit approval"):
        briefing.draft_briefing(
            root,
            now=NOW + timedelta(hours=1),
            language="en",
            connector_failed=True,
        )


def test_empty_brief_evidence_and_disposition_validation(tmp_path: Path) -> None:
    root = ready_workspace(tmp_path)
    empty = briefing.draft_briefing(root, now=NOW, language="bilingual")
    assert empty["outcome"] == "no_candidates"
    assert briefing._render_markdown("2026-06-20", "en", [], {}) == (
        "# Daily CEO Brief · 2026-06-20\n\nNo decision-required items.\n"
    )
    assert "Daily CEO Brief / 今日 CEO 简报" in briefing._render_markdown(
        "2026-06-20", "bilingual", [], {}
    )
    current = root / "data" / "briefs" / "brief.current.json"
    current.write_text('{"evidence_hashes":[]}\n')
    assert briefing._last_evidence(root) == {}

    invalid_values = [None, {"kind": 1}, {"kind": "unknown"}, {"kind": "act"}]
    for value in invalid_values:
        with pytest.raises(briefing.BriefingError):
            briefing._validate_disposition(value, "T1")
    with pytest.raises(briefing.BriefingError, match="empty"):
        briefing._validate_disposition({"kind": "dismiss", "reason": " "}, "T1")
    assert (
        briefing._validate_disposition(
            {"kind": "delegate", "owner_alias": "ops", "review_date": "2026-06-22"}, "T1"
        )["kind"]
        == "delegate"
    )


def test_render_unknown_token_and_apply_tampering(tmp_path: Path) -> None:
    root = ready_workspace(tmp_path)
    secure_replace(
        root / "data" / "risks" / "risk_one.json",
        canonical_json_bytes(record("risk", "risk_one", severity="high")),
    )
    drafted = briefing.draft_briefing(root, now=NOW, language="en")
    run_id = drafted["run_id"]
    assert isinstance(run_id, str)
    with pytest.raises(briefing.BriefingError, match="unknown disposition"):
        briefing.render_briefing(
            root,
            run_id,
            {
                "T1": {"kind": "dismiss", "reason": "accepted"},
                "T2": {"kind": "dismiss", "reason": "unknown"},
            },
        )
    rendered = briefing.render_briefing(
        root, run_id, {"T1": {"kind": "dismiss", "reason": "accepted"}}
    )
    with pytest.raises(briefing.BriefingError, match="token mismatch"):
        briefing.apply_briefing(root, run_id, confirm="wrong", now=NOW)
    run_root = root / "inbox" / "briefings" / run_id
    (run_root / "brief.md").write_text("tampered\n")
    with pytest.raises(briefing.BriefingError, match="seal mismatch"):
        briefing.apply_briefing(
            root,
            run_id,
            confirm=rendered["approval_token"],
            now=NOW,
        )
