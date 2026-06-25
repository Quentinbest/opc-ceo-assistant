from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import resource
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from opc_ceo.briefing import _candidate, _ranking
from opc_ceo.contracts import canonical_json_bytes
from opc_ceo.diagnostics import workspace_status
from opc_ceo.workspace import initialize_workspace, secure_append, secure_mkdir, secure_replace

PHASES = ("normalize", "apply", "briefing", "status")
BUDGETS = {
    "normalize": {"elapsed_ms": 3_000.0, "rss_mib": 150.0},
    "apply": {"elapsed_ms": 15_000.0, "rss_mib": 200.0},
    "briefing": {"elapsed_ms": 2_000.0, "rss_mib": 100.0},
    "status": {"elapsed_ms": 2_000.0, "rss_mib": 100.0},
}


def nearest_rank_percentile(samples: Sequence[float], percentile: float) -> float:
    if not samples:
        raise ValueError("samples must not be empty")
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in (0, 1]")
    ordered = sorted(samples)
    return float(ordered[math.ceil(percentile * len(ordered)) - 1])


def _rss_mib() -> float:
    peak = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return peak / divisor


def _synthetic_record(index: int) -> dict[str, Any]:
    return {
        "record_id": f"priority-{index:04d}",
        "type": "priority",
        "title": f"Priority {index}",
        "status": "active",
        "target_date": "2026-06-20",
        "weight": index % 5 + 1,
        "updated_at": "2026-06-20T00:00:00Z",
    }


def _prepare_status_workspace(root: Path, *, records: int) -> None:
    initialize_workspace(root, approved=True)
    for index in range(records):
        record_id = f"priority_{index:04d}"
        projection = {"record_id": record_id, "pending": None}
        secure_replace(
            root / "data" / "source_projection" / f"{record_id}.json",
            canonical_json_bytes(projection),
        )

    run_count = min(records, 20)
    for index in range(run_count):
        import_id = f"benchmark-import-{index:04d}"
        import_root = root / "inbox" / "imports" / import_id
        secure_mkdir(import_root)
        secure_replace(
            import_root / "envelope.json",
            canonical_json_bytes(
                {
                    "exported_at": f"2026-06-20T00:{index:02d}:00Z",
                    "degraded": False,
                    "drive": {"version": str(index + 1)},
                }
            ),
        )
        secure_replace(import_root / "seal.json", canonical_json_bytes({"seal_sha256": "seal"}))
        secure_replace(
            import_root / "apply_result.json",
            canonical_json_bytes({"outcome": "applied"}),
        )

        brief_id = f"benchmark-brief-{index:04d}"
        brief_root = root / "inbox" / "briefings" / brief_id
        secure_mkdir(brief_root)
        secure_replace(
            brief_root / "candidate_set.json",
            canonical_json_bytes({"brief_date": "2026-06-20"}),
        )
        secure_replace(
            brief_root / "brief.json",
            canonical_json_bytes({"brief_date": "2026-06-20"}),
        )
        secure_replace(brief_root / "dispositions.json", canonical_json_bytes({}))
        secure_replace(brief_root / "seal.json", canonical_json_bytes({"seal_sha256": "seal"}))
        secure_replace(
            brief_root / "apply_result.json",
            canonical_json_bytes(
                {
                    "outcome": "applied",
                    "brief_revision_id": f"brief_20260620_daily_r{index + 1:03d}",
                }
            ),
        )

        for event in (
            {"event": "apply_started", "kind": "import", "run_id": import_id},
            {"event": "apply_completed", "kind": "import", "run_id": import_id},
            {"event": "apply_started", "kind": "briefing", "run_id": brief_id},
            {"event": "apply_completed", "kind": "briefing", "run_id": brief_id},
        ):
            secure_append(root / "logs" / "events.jsonl", canonical_json_bytes(event))


def worker_sample(phase: str, *, records: int) -> dict[str, float]:
    if phase not in PHASES:
        raise ValueError(f"unknown phase: {phase}")
    with tempfile.TemporaryDirectory(prefix="opc-benchmark-") as temporary:
        root = Path(temporary)
        if phase == "status":
            _prepare_status_workspace(root, records=records)
        started = time.perf_counter()
        if phase == "normalize":
            for index in range(records):
                canonical_json_bytes(_synthetic_record(index))
        elif phase == "apply":
            destination = root / "data"
            for index in range(records):
                secure_replace(
                    destination / f"priority-{index:04d}.json",
                    canonical_json_bytes(_synthetic_record(index)),
                )
        elif phase == "briefing":
            candidates = (
                _candidate(
                    _synthetic_record(index),
                    today=date(2026, 6, 20),
                    thresholds={"CNY": "10000.00", "USD": "1500.00"},
                    last_evidence={},
                )
                for index in range(records)
            )
            sorted((item for item in candidates if item is not None), key=_ranking)
        else:
            workspace_status(root)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
    return {
        "elapsed_ms": elapsed_ms,
        "rss_mib": _rss_mib(),
    }


def _run_sample(phase: str, records: int) -> dict[str, float]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "opc_ceo.benchmark",
            "--worker",
            phase,
            "--records",
            str(records),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(completed.stdout)
    return {"elapsed_ms": float(value["elapsed_ms"]), "rss_mib": float(value["rss_mib"])}


def environment_metadata() -> dict[str, Any]:
    lock_path = Path(__file__).resolve().parents[2] / "uv.lock"
    lock_sha256 = (
        hashlib.sha256(lock_path.read_bytes()).hexdigest() if lock_path.is_file() else None
    )
    total_memory = None
    if hasattr(os, "sysconf"):
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
        total_memory = int(page_size * page_count)
    return {
        "os": platform.platform(),
        "architecture": platform.machine(),
        "cpu": platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
        "ram_bytes": total_memory,
        "python": platform.python_version(),
        "lock_sha256": lock_sha256,
        "power_mode": "not-controlled",
    }


def run_benchmarks(
    *, phases: Sequence[str], records: int, warmup: int, repeat: int
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    passed = True
    for phase in phases:
        if phase not in PHASES:
            raise ValueError(f"unknown phase: {phase}")
        for _ in range(warmup):
            _run_sample(phase, records)
        samples = [_run_sample(phase, records) for _ in range(repeat)]
        elapsed = nearest_rank_percentile([sample["elapsed_ms"] for sample in samples], 0.95)
        rss = nearest_rank_percentile([sample["rss_mib"] for sample in samples], 0.95)
        budget = BUDGETS[phase]
        phase_passed = elapsed < budget["elapsed_ms"] and rss < budget["rss_mib"]
        passed = passed and phase_passed
        results[phase] = {
            "samples": samples,
            "p95_elapsed_ms": elapsed,
            "p95_rss_mib": rss,
            "budget_elapsed_ms": budget["elapsed_ms"],
            "budget_rss_mib": budget["rss_mib"],
            "passed": phase_passed,
        }
    return {
        "schema_version": "1.0.0",
        "environment": environment_metadata(),
        "records": records,
        "warmup": warmup,
        "repeat": repeat,
        "stat": "nearest-rank-p95",
        "phases": results,
        "passed": passed,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run reproducible OPC CEO benchmarks")
    parser.add_argument("--phase", choices=("all", *PHASES), default="all")
    parser.add_argument("--records", type=int, default=1000)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--repeat", type=int, default=20)
    parser.add_argument("--stat", choices=("p95",), default="p95")
    parser.add_argument("--worker", choices=PHASES, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker:
        print(json.dumps(worker_sample(args.worker, records=args.records), sort_keys=True))
        return 0
    phases = PHASES if args.phase == "all" else (args.phase,)
    result = run_benchmarks(
        phases=phases,
        records=args.records,
        warmup=args.warmup,
        repeat=args.repeat,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
