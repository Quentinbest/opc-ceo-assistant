from __future__ import annotations

from pathlib import Path

from evals.run import CASES_ROOT, load_cases, run_case
from evals.verify_output import verify_case, verify_report


def test_case_inventory_contains_ten_unique_forward_evals() -> None:
    cases = load_cases(CASES_ROOT)
    assert len(cases) == 10
    assert len({case["id"] for case in cases}) == 10


def test_verifier_scores_assertions_and_enforces_automatic_failures() -> None:
    case = {"id": "sample", "required_assertions": ["a", "b"]}
    passed = verify_case(case, {"assertions": {"a": True, "b": True}, "auto_failures": []})
    assert passed["score"] == 100
    assert passed["passed"] is True

    failed = verify_case(
        case,
        {"assertions": {"a": True, "b": True}, "auto_failures": ["unsealed_apply"]},
    )
    assert failed["score"] == 100
    assert failed["passed"] is False
    assert verify_report([passed, failed])["passed"] is False


def test_each_forward_eval_runs_in_an_isolated_workspace(tmp_path: Path) -> None:
    results = [run_case(case, tmp_path / case["id"]) for case in load_cases(CASES_ROOT)]
    report = verify_report(results)
    assert report["case_count"] == 10
    assert report["passed"] is True
    assert all(result["score"] >= 90 for result in report["results"])
