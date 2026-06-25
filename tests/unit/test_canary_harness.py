from __future__ import annotations

from pathlib import Path

from opc_ceo.contracts import generate_resources
from spikes.canary import run_canaries, verify_canary_receipt


def test_canary_runs_full_empty_workbook_loop_and_verifies(tmp_path: Path) -> None:
    source = generate_resources(tmp_path / "resources")["workbook"]
    receipt = run_canaries(
        source=source,
        workspace_root=tmp_path / "runs",
        spreadsheet_id="disposable-copy",
        revision_before="1",
        revision_after="1",
        modified_time="2026-06-20T01:00:00Z",
        count=1,
        copied_workbook_deleted=True,
    )
    assert receipt["status"] == "passed"
    assert receipt["spreadsheet_id_hash"].startswith("sha256:")
    assert "disposable-copy" not in str(receipt)
    assert verify_canary_receipt(receipt, expected_count=1) == (True, [])


def test_canary_verifier_rejects_incomplete_evidence() -> None:
    valid, diagnostics = verify_canary_receipt({"status": "failed"}, expected_count=5)
    assert valid is False
    assert diagnostics[0]["code"] == "CANARY_EVIDENCE_ERROR"
