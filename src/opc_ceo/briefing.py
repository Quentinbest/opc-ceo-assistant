from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from opc_ceo.contracts import canonical_json_bytes
from opc_ceo.workspace import (
    FaultPoint,
    ensure_supported_workspace,
    event_exists,
    fault_checkpoint,
    load_manifest,
    resolve_workspace_path,
    secure_append,
    secure_mkdir,
    secure_replace,
    secure_write,
    secure_write_once,
    workspace_lock,
)

REASON_ORDER = (
    "SEVERITY_CRITICAL",
    "SEVERITY_HIGH",
    "SEVERITY_MEDIUM",
    "SEVERITY_LOW",
    "DUE_OVERDUE",
    "DUE_TODAY",
    "DUE_1_3_DAYS",
    "DUE_4_7_DAYS",
    "BLOCKED",
    "DISPUTED",
    "DELTA_WORSENED",
    "DELTA_REOPENED",
    "DELTA_NEW",
    "AMOUNT_AT_OR_ABOVE_THRESHOLD",
    "STRATEGIC_WEIGHT_1",
    "STRATEGIC_WEIGHT_2",
    "STRATEGIC_WEIGHT_3",
    "STRATEGIC_WEIGHT_4",
    "STRATEGIC_WEIGHT_5",
)
REASON_INDEX = {value: index for index, value in enumerate(REASON_ORDER)}
SEVERITY_POINTS = {"critical": 40, "high": 30, "medium": 15, "low": 5}
SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
DUE_POINTS = {"overdue": 30, "today": 25, "1_3_days": 15, "4_7_days": 5}
DOMAIN_DIRECTORIES = ("priorities", "pipeline", "receivables", "contracts", "projects", "risks")


class BriefingError(ValueError):
    pass


@dataclass(frozen=True)
class Candidate:
    record_id: str
    domain: str
    score: int
    reason_codes: tuple[str, ...]
    severity: str | None
    due: str | None
    due_bucket: str | None
    strategic_weight: int
    amount: str | None
    currency: str | None
    delta: str | None
    mandatory: bool
    updated_at: str
    source_hash: str


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BriefingError(f"expected object: {path.name}")
    return value


def _load_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for directory in DOMAIN_DIRECTORIES:
        for path in sorted(resolve_workspace_path(root, f"data/{directory}").glob("*.json")):
            record = _read_json(path)
            status = str(record.get("status", "")).lower()
            resolved = (
                bool(record.get("archived_at"))
                or status == "archived"
                or (record.get("type") == "pipeline" and status in {"won", "lost"})
                or (
                    record.get("type") == "receivable"
                    and Decimal(record.get("outstanding_amount") or "0") <= 0
                )
                or (record.get("type") == "project" and status in {"completed", "done"})
                or (record.get("type") == "risk" and status in {"closed", "resolved"})
                or (record.get("type") == "contract" and status in {"expired", "terminated"})
            )
            if not resolved:
                records.append(record)
    return records


def _due_and_severity(record: dict[str, Any], today: date) -> tuple[date | None, str | None]:
    domain = record["type"]
    due: date | None = None
    severity: str | None = None
    if domain == "priority" and record.get("target_date"):
        due = date.fromisoformat(record["target_date"])
    elif domain == "pipeline" and record.get("next_action_due"):
        due = date.fromisoformat(record["next_action_due"])
    elif domain == "receivable" and Decimal(record.get("outstanding_amount") or "0") > 0:
        due = date.fromisoformat(record["due_date"])
        days = (today - due).days
        if days > 30:
            severity = "critical"
        elif days > 7:
            severity = "high"
        elif days > 0:
            severity = "medium"
    elif domain == "contract":
        dates = [
            date.fromisoformat(record[field])
            for field in ("review_date", "signature_date", "renewal_date")
            if record.get(field)
        ]
        due = min(dates) if dates else None
        severity = record.get("risk_level")
    elif domain == "project":
        due = date.fromisoformat(record["milestone_due"]) if record.get("milestone_due") else None
        severity = record.get("health")
        if severity == "healthy":
            severity = None
    elif domain == "risk":
        dates = [
            date.fromisoformat(record[field])
            for field in ("due_date", "mitigation_date")
            if record.get(field)
        ]
        due = min(dates) if dates else None
        severity = record.get("severity")
    return due, severity if severity in SEVERITY_POINTS else None


def _due_bucket(due: date | None, today: date) -> str | None:
    if due is None:
        return None
    days = (due - today).days
    if days < 0:
        return "overdue"
    if days == 0:
        return "today"
    if days <= 3:
        return "1_3_days"
    if days <= 7:
        return "4_7_days"
    return None


def _last_evidence(root: Path) -> dict[str, str]:
    current = sorted(resolve_workspace_path(root, "data/briefs").glob("*.current.json"))
    if not current:
        return {}
    brief = _read_json(current[-1])
    evidence = brief.get("evidence_hashes", {})
    return evidence if isinstance(evidence, dict) else {}


def _candidate(
    record: dict[str, Any],
    *,
    today: date,
    thresholds: dict[str, str],
    last_evidence: dict[str, str],
) -> Candidate | None:
    due, severity = _due_and_severity(record, today)
    bucket = _due_bucket(due, today)
    reasons: list[str] = []
    score = 0
    if severity:
        score += SEVERITY_POINTS[severity]
        reasons.append(f"SEVERITY_{severity.upper()}")
    if bucket:
        score += DUE_POINTS[bucket]
        reasons.append(f"DUE_{bucket.upper()}")
    if record.get("blocked") is True:
        score += 15
        reasons.append("BLOCKED")
    if record.get("disputed") is True:
        score += 15
        reasons.append("DISPUTED")
    source_hash = _hash(canonical_json_bytes(record))
    delta = None
    if record["record_id"] not in last_evidence:
        delta = "new"
        score += 5
        reasons.append("DELTA_NEW")
    elif last_evidence[record["record_id"]] != source_hash:
        delta = "changed"
    amount_field = "outstanding_amount" if record["type"] == "receivable" else "amount"
    amount = record.get(amount_field)
    currency = record.get("currency")
    if amount is not None and currency is not None:
        threshold = thresholds.get(currency)
        if threshold is None:
            raise BriefingError(f"missing approval threshold for {currency}")
        if Decimal(amount) >= Decimal(threshold):
            score += 10
            reasons.append("AMOUNT_AT_OR_ABOVE_THRESHOLD")
    weight = int(record.get("weight") or record.get("strategic_weight") or 1)
    score += weight * 4
    reasons.append(f"STRATEGIC_WEIGHT_{weight}")
    ordered_reasons = tuple(sorted(set(reasons), key=REASON_INDEX.__getitem__))
    if score < 5 or not ordered_reasons:
        return None
    mandatory = bucket in {"overdue", "today"} or severity in {"high", "critical"}
    return Candidate(
        record_id=record["record_id"],
        domain=record["type"],
        score=score,
        reason_codes=ordered_reasons,
        severity=severity,
        due=due.isoformat() if due else None,
        due_bucket=bucket,
        strategic_weight=weight,
        amount=amount,
        currency=currency,
        delta=delta,
        mandatory=mandatory,
        updated_at=record["updated_at"],
        source_hash=source_hash,
    )


def _ranking(candidate: Candidate) -> tuple[Any, ...]:
    due_key = date.fromisoformat(candidate.due).toordinal() if candidate.due else 10**9
    updated = datetime.fromisoformat(candidate.updated_at.replace("Z", "+00:00"))
    return (
        -int(candidate.mandatory),
        -candidate.score,
        -SEVERITY_RANK.get(candidate.severity or "", 0),
        due_key,
        -candidate.strategic_weight,
        -updated.timestamp(),
        candidate.record_id,
    )


def _candidate_dict(candidate: Candidate) -> dict[str, Any]:
    return {
        "record_id": candidate.record_id,
        "domain": candidate.domain,
        "score": candidate.score,
        "reason_codes": list(candidate.reason_codes),
        "severity": candidate.severity,
        "due": candidate.due,
        "due_bucket": candidate.due_bucket,
        "strategic_weight": candidate.strategic_weight,
        "amount": candidate.amount,
        "currency": candidate.currency,
        "delta": candidate.delta,
        "mandatory": candidate.mandatory,
        "updated_at": candidate.updated_at,
        "source_hash": candidate.source_hash,
    }


def _host_view(candidate: Candidate, token: str) -> dict[str, Any]:
    return {
        "token": token,
        "type": candidate.domain,
        "severity": candidate.severity,
        "due": candidate.due,
        "due_bucket": candidate.due_bucket,
        "amount": candidate.amount,
        "currency": candidate.currency,
        "score": candidate.score,
        "reason_codes": list(candidate.reason_codes),
        "strategic_weight": candidate.strategic_weight,
        "delta": candidate.delta,
    }


def draft_briefing(
    root: Path,
    *,
    now: datetime,
    language: str,
    connector_failed: bool = False,
    allow_stale: bool = False,
) -> dict[str, Any]:
    ensure_supported_workspace(root)
    if language not in {"zh-CN", "en", "bilingual"}:
        raise BriefingError("language must be zh-CN, en, or bilingual")
    manifest = load_manifest(root)
    refreshed = manifest["source"].get("last_successful_refresh_at")
    if not refreshed:
        raise BriefingError("no successfully refreshed source")
    refreshed_at = datetime.fromisoformat(refreshed.replace("Z", "+00:00"))
    stale_seconds = (now.astimezone(UTC) - refreshed_at.astimezone(UTC)).total_seconds()
    max_stale = int(manifest["limits"]["max_stale_hours"]) * 3600
    if stale_seconds > max_stale:
        return {
            "outcome": "stale_too_old",
            "run_id": None,
            "top_approval_view": [],
            "diagnostics": [{"code": "STALE_SOURCE_TOO_OLD", "age_seconds": int(stale_seconds)}],
        }
    if connector_failed and not allow_stale:
        raise BriefingError("STALE_SOURCE requires explicit approval")

    records = _load_records(root)
    if len(records) > int(manifest["limits"]["records"]):
        raise BriefingError("record limit exceeded")
    candidates = [
        candidate
        for record in records
        if (
            candidate := _candidate(
                record,
                today=now.date(),
                thresholds=manifest["approval_amount_thresholds"],
                last_evidence=_last_evidence(root),
            )
        )
        is not None
    ]
    candidates.sort(key=_ranking)
    mandatory_count = sum(candidate.mandatory for candidate in candidates)
    triage_limit = int(manifest["limits"]["triage_display_limit"])
    if mandatory_count > triage_limit:
        return {
            "outcome": "triage_only",
            "run_id": None,
            "top_approval_view": [],
            "diagnostics": [{"code": "CANDIDATE_OVERFLOW", "mandatory_count": mandatory_count}],
        }
    top = candidates[: min(3, len(candidates))]
    tokens = {f"T{index}": candidate.record_id for index, candidate in enumerate(top, 1)}
    candidate_payload = {
        "schema_version": "1.0.0",
        "brief_date": now.date().isoformat(),
        "language": language,
        "source": {
            "drive_version": manifest["source"]["last_fully_applied_drive_version"],
            "stale": connector_failed,
            "age_seconds": int(stale_seconds),
        },
        "candidates": [_candidate_dict(candidate) for candidate in candidates],
        "top_tokens": tokens,
    }
    content = canonical_json_bytes(candidate_payload)
    run_id = f"briefing_{now.astimezone(UTC):%Y%m%dT%H%M%SZ}_{_hash(content)[:10]}"
    run_root = resolve_workspace_path(root, f"inbox/briefings/{run_id}")
    secure_mkdir(run_root)
    secure_write(run_root / "candidate_set.json", content, exclusive=True)
    outcome = (
        "no_candidates" if not candidates else ("stale_drafted" if connector_failed else "drafted")
    )
    return {
        "outcome": outcome,
        "run_id": run_id,
        "top_approval_view": [
            _host_view(candidate, f"T{index}") for index, candidate in enumerate(top, 1)
        ],
        "diagnostics": [],
    }


def _validate_disposition(value: object, token: str) -> dict[str, str]:
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        raise BriefingError(f"invalid disposition: {token}")
    kind = value["kind"]
    required = {
        "act": {"kind", "next_action"},
        "defer": {"kind", "review_date"},
        "delegate": {"kind", "owner_alias", "review_date"},
        "dismiss": {"kind", "reason"},
    }
    if kind not in required or set(value) != required[kind]:
        raise BriefingError(f"invalid disposition fields: {token}")
    result = {key: str(item) for key, item in value.items()}
    if kind in {"defer", "delegate"}:
        date.fromisoformat(result["review_date"])
    if any(not item.strip() for item in result.values()):
        raise BriefingError(f"empty disposition field: {token}")
    return result


def _render_markdown(
    brief_date: str,
    language: str,
    top: list[dict[str, Any]],
    dispositions: dict[str, dict[str, str]],
) -> str:
    def section(title: str) -> list[str]:
        lines = [f"# {title}", ""]
        if not top:
            lines.extend(
                ["No decision-required items." if language == "en" else "今日无待决事项。", ""]
            )
        for item in top:
            token = item["token"]
            disposition = dispositions[token]
            lines.extend(
                [
                    f"## {token} · {item['type']}",
                    f"- Score: {item['score']}",
                    f"- Reasons: {', '.join(item['reason_codes'])}",
                    f"- Disposition: {disposition['kind']}",
                ]
            )
            for key, value in disposition.items():
                if key != "kind":
                    lines.append(f"- {key}: {value}")
            lines.append("")
        return lines

    if language == "en":
        lines = section(f"Daily CEO Brief · {brief_date}")
    elif language == "zh-CN":
        lines = section(f"今日 CEO 简报 · {brief_date}")
    else:
        lines = section(f"Daily CEO Brief / 今日 CEO 简报 · {brief_date}")
    return "\n".join(lines).rstrip() + "\n"


def _seal(run_root: Path, paths: list[str]) -> dict[str, Any]:
    entries = [
        {"path": name, "sha256": _hash((run_root / name).read_bytes())} for name in sorted(paths)
    ]
    body = {"schema_version": "1.0.0", "entries": entries}
    return {**body, "seal_sha256": _hash(canonical_json_bytes(body))}


def render_briefing(root: Path, run_id: str, dispositions: dict[str, object]) -> dict[str, Any]:
    ensure_supported_workspace(root)
    run_root = resolve_workspace_path(root, f"inbox/briefings/{run_id}")
    candidate_set = _read_json(run_root / "candidate_set.json")
    tokens = candidate_set["top_tokens"]
    if set(dispositions) != set(tokens):
        missing = sorted(set(tokens) - set(dispositions))
        unknown = sorted(set(dispositions) - set(tokens))
        if missing:
            raise BriefingError(f"missing disposition: {', '.join(missing)}")
        raise BriefingError(f"unknown disposition: {', '.join(unknown)}")
    validated = {token: _validate_disposition(dispositions[token], token) for token in tokens}
    by_id = {item["record_id"]: item for item in candidate_set["candidates"]}
    top = [
        {
            "token": token,
            "type": by_id[record_id]["domain"],
            "score": by_id[record_id]["score"],
            "reason_codes": by_id[record_id]["reason_codes"],
        }
        for token, record_id in tokens.items()
    ]
    brief = {
        "schema_version": "1.0.0",
        "brief_date": candidate_set["brief_date"],
        "language": candidate_set["language"],
        "source": candidate_set["source"],
        "top": top,
        "dispositions": validated,
        "evidence_hashes": {
            record_id: by_id[record_id]["source_hash"] for record_id in tokens.values()
        },
    }
    markdown = _render_markdown(
        candidate_set["brief_date"], candidate_set["language"], top, validated
    )
    secure_write(run_root / "dispositions.json", canonical_json_bytes(validated), exclusive=True)
    secure_write(run_root / "brief.json", canonical_json_bytes(brief), exclusive=True)
    secure_write(run_root / "brief.md", markdown.encode(), exclusive=True)
    seal = _seal(run_root, ["candidate_set.json", "dispositions.json", "brief.json", "brief.md"])
    secure_write(run_root / "seal.json", canonical_json_bytes(seal), exclusive=True)
    return {
        "outcome": "sealed",
        "run_id": run_id,
        "approval_token": f"{run_id}:{seal['seal_sha256']}",
        "brief_markdown": markdown,
    }


def _verify_seal(run_root: Path, seal: dict[str, Any]) -> None:
    expected = _seal(run_root, [entry["path"] for entry in seal["entries"]])
    if expected != seal:
        raise BriefingError("approval seal mismatch")


def apply_briefing(root: Path, run_id: str, *, confirm: str, now: datetime) -> dict[str, Any]:
    ensure_supported_workspace(root)
    run_root = resolve_workspace_path(root, f"inbox/briefings/{run_id}")
    seal = _read_json(run_root / "seal.json")
    if confirm != f"{run_id}:{seal['seal_sha256']}":
        raise BriefingError("approval token mismatch")
    result_path = run_root / "apply_result.json"
    if result_path.exists():
        if not event_exists(root, "apply_completed", "briefing", run_id):
            secure_append(
                root / "logs" / "events.jsonl",
                canonical_json_bytes(
                    {
                        "event": "apply_completed",
                        "kind": "briefing",
                        "run_id": run_id,
                        "at": now.astimezone(UTC).isoformat(),
                        "recovered": True,
                    }
                ),
            )
        return {"outcome": "already_applied", "run_id": run_id}
    _verify_seal(run_root, seal)
    brief = _read_json(run_root / "brief.json")
    candidate_set = _read_json(run_root / "candidate_set.json")
    dispositions = _read_json(run_root / "dispositions.json")
    brief_date = brief["brief_date"]
    with workspace_lock(root, "briefing-apply"):
        journal_path = run_root / "apply_journal.json"
        if journal_path.exists():
            journal = _read_json(journal_path)
        else:
            existing = sorted(
                resolve_workspace_path(root, "data/briefs").glob(
                    f"brief_{brief_date.replace('-', '')}_daily_r*.json"
                )
            )
            journal = {
                "schema_version": "1.0.0",
                "kind": "briefing",
                "run_id": run_id,
                "revision": len(existing) + 1,
                "applied_at": now.astimezone(UTC).isoformat(),
            }
            fault_checkpoint(FaultPoint.BRIEF_JOURNAL_WRITE)
            secure_write(journal_path, canonical_json_bytes(journal), exclusive=True)
        revision = int(journal["revision"])
        now = datetime.fromisoformat(str(journal["applied_at"]).replace("Z", "+00:00"))
        revision_id = f"brief_{brief_date.replace('-', '')}_daily_r{revision:03d}"
        immutable_brief = {
            **brief,
            "brief_revision_id": revision_id,
            "applied_at": now.astimezone(UTC).isoformat(),
        }
        if not event_exists(root, "apply_started", "briefing", run_id):
            fault_checkpoint(FaultPoint.BRIEF_APPLY_STARTED)
            secure_append(
                root / "logs" / "events.jsonl",
                canonical_json_bytes(
                    {
                        "event": "apply_started",
                        "kind": "briefing",
                        "run_id": run_id,
                        "at": now.astimezone(UTC).isoformat(),
                        "seal": seal["seal_sha256"],
                    }
                ),
            )
        fault_checkpoint(FaultPoint.BRIEF_REVISION_WRITE)
        secure_write_once(
            root / "data" / "briefs" / f"{revision_id}.json",
            canonical_json_bytes(immutable_brief),
        )
        markdown = (run_root / "brief.md").read_bytes()
        fault_checkpoint(FaultPoint.BRIEF_MARKDOWN_WRITE)
        secure_write_once(
            root / "briefs" / "daily" / f"{brief_date}-r{revision:03d}.md",
            markdown,
        )
        for token, record_id in candidate_set["top_tokens"].items():
            decision = {
                "schema_version": "1.0.0",
                "decision_id": f"decision_{brief_date}_{token.lower()}_r{revision:03d}",
                "brief_revision_id": revision_id,
                "candidate_record_id": record_id,
                "token": token,
                "disposition": dispositions[token],
                "decided_at": now.astimezone(UTC).isoformat(),
            }
            fault_checkpoint(FaultPoint.BRIEF_DECISION_WRITE)
            secure_write_once(
                root / "data" / "decisions" / f"{decision['decision_id']}.json",
                canonical_json_bytes(decision),
            )
        fault_checkpoint(FaultPoint.BRIEF_POINTER_WRITE)
        secure_replace(
            root / "data" / "briefs" / f"brief_{brief_date.replace('-', '')}_daily.current.json",
            canonical_json_bytes(immutable_brief),
        )
        secure_replace(root / "briefs" / "daily" / f"{brief_date}.md", markdown)
        result = {"outcome": "applied", "run_id": run_id, "brief_revision_id": revision_id}
        fault_checkpoint(FaultPoint.BRIEF_RESULT_WRITE)
        secure_write_once(result_path, canonical_json_bytes(result))
        if not event_exists(root, "apply_completed", "briefing", run_id):
            fault_checkpoint(FaultPoint.BRIEF_APPLY_COMPLETED)
            secure_append(
                root / "logs" / "events.jsonl",
                canonical_json_bytes(
                    {
                        "event": "apply_completed",
                        "kind": "briefing",
                        "run_id": run_id,
                        "at": now.astimezone(UTC).isoformat(),
                    }
                ),
            )
    return result
