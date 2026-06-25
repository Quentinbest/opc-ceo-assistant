from __future__ import annotations

import json
from pathlib import Path

import pytest

import opc_ceo.benchmark as benchmark
from opc_ceo.diagnostics import workspace_status


def test_nearest_rank_percentile() -> None:
    assert benchmark.nearest_rank_percentile(list(range(1, 21)), 0.95) == 19
    with pytest.raises(ValueError, match="samples"):
        benchmark.nearest_rank_percentile([], 0.95)
    with pytest.raises(ValueError, match="percentile"):
        benchmark.nearest_rank_percentile([1], 0)


def test_run_benchmarks_aggregates_samples_and_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    samples = iter({"elapsed_ms": float(index), "rss_mib": 50.0} for index in range(1, 13))
    monkeypatch.setattr(benchmark, "_run_sample", lambda phase, records: next(samples))
    monkeypatch.setattr(
        benchmark,
        "environment_metadata",
        lambda: {"os": "test", "python": "3.12", "lock_sha256": "hash"},
    )

    result = benchmark.run_benchmarks(
        phases=("normalize", "status"), records=1000, warmup=1, repeat=5
    )

    assert result["environment"]["os"] == "test"
    assert result["phases"]["normalize"]["p95_elapsed_ms"] == 6.0
    assert result["phases"]["status"]["p95_elapsed_ms"] == 12.0
    assert result["passed"] is True


def test_budget_failure_and_main_json(monkeypatch: pytest.MonkeyPatch, capsys: object) -> None:
    monkeypatch.setattr(
        benchmark,
        "run_benchmarks",
        lambda **kwargs: {
            "schema_version": "1.0.0",
            "passed": False,
            "environment": {},
            "phases": {},
        },
    )
    assert (
        benchmark.main(["--phase", "all", "--records", "10", "--warmup", "0", "--repeat", "1"]) == 1
    )
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["passed"] is False


def test_worker_sample_runs_real_status_phase() -> None:
    sample = benchmark.worker_sample("status", records=1)
    assert sample["elapsed_ms"] >= 0
    assert sample["rss_mib"] > 0
    with pytest.raises(ValueError, match="phase"):
        benchmark.worker_sample("unknown", records=1)


def test_status_benchmark_fixture_scales_records_and_bounds_run_history(tmp_path: Path) -> None:
    root = tmp_path / "workspace"

    benchmark._prepare_status_workspace(root, records=25)
    status = workspace_status(root)

    assert len(list((root / "data" / "source_projection").glob("*.json"))) == 25
    assert status["imports"]["runs"] == 20
    assert status["briefings"]["runs"] == 20
    assert status["audit"]["valid_events"] == 80
    assert status["outcome"] == "healthy"


@pytest.mark.parametrize("phase", ["normalize", "apply", "briefing"])
def test_worker_sample_runs_each_data_phase(phase: str) -> None:
    sample = benchmark.worker_sample(phase, records=2)
    assert sample["elapsed_ms"] >= 0
    assert sample["rss_mib"] > 0


def test_run_sample_uses_isolated_worker() -> None:
    sample = benchmark._run_sample("status", 1)
    assert sample["elapsed_ms"] >= 0
    assert sample["rss_mib"] > 0


def test_environment_metadata_handles_portable_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = benchmark.environment_metadata()
    assert metadata["lock_sha256"]
    assert metadata["ram_bytes"] > 0

    monkeypatch.setattr("opc_ceo.benchmark.sys.platform", "linux")
    assert benchmark._rss_mib() > 0
    monkeypatch.setattr("opc_ceo.benchmark.platform.processor", lambda: "")
    monkeypatch.delattr("opc_ceo.benchmark.os.sysconf")

    class MissingLockPath(type(Path())):  # type: ignore[misc]
        def is_file(self) -> bool:
            return False

    monkeypatch.setattr("opc_ceo.benchmark.Path", MissingLockPath)
    fallback = benchmark.environment_metadata()
    assert fallback["lock_sha256"] is None
    assert fallback["ram_bytes"] is None
    assert fallback["cpu"] == "unknown"


def test_run_benchmarks_rejects_phase_and_detects_budget_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="phase"):
        benchmark.run_benchmarks(phases=("unknown",), records=1, warmup=0, repeat=1)

    monkeypatch.setattr(
        benchmark,
        "_run_sample",
        lambda phase, records: {"elapsed_ms": 20_000.0, "rss_mib": 250.0},
    )
    result = benchmark.run_benchmarks(phases=("apply",), records=1, warmup=0, repeat=1)
    assert result["passed"] is False


def test_main_worker_and_single_phase(monkeypatch: pytest.MonkeyPatch, capsys: object) -> None:
    monkeypatch.setattr(
        benchmark,
        "worker_sample",
        lambda phase, records: {"elapsed_ms": float(records), "rss_mib": 1.0},
    )
    assert benchmark.main(["--worker", "status", "--records", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["elapsed_ms"] == 2.0  # type: ignore[attr-defined]

    monkeypatch.setattr(
        benchmark,
        "run_benchmarks",
        lambda **kwargs: {"passed": True, "phases": kwargs["phases"]},
    )
    assert benchmark.main(["--phase", "status", "--repeat", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["phases"] == ["status"]  # type: ignore[attr-defined]
