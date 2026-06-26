from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from opc_ceo.contracts import CONTRACT_VERSION, RESOURCE_ROOT

POLICY_PATH = (
    RESOURCE_ROOT / "contracts" / CONTRACT_VERSION / "domain-version-policy.json"
)


class DomainVersionError(ValueError):
    pass


def load_domain_version_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != CONTRACT_VERSION:
        raise DomainVersionError("unsupported domain version policy")
    return cast(dict[str, Any], value)


def _record_policy(record_type: str) -> dict[str, Any]:
    policy = load_domain_version_policy()
    record_types = cast(dict[str, dict[str, Any]], policy["record_types"])
    try:
        return record_types[record_type]
    except KeyError as error:
        raise DomainVersionError(f"unknown record type: {record_type}") from error


def classify_record_type(record_type: str) -> str:
    return str(_record_policy(record_type)["classification"])


def _integer_revision(record: dict[str, object], field: str) -> int:
    value = record.get(field)
    if not isinstance(value, int):
        raise DomainVersionError(f"{field} must be an integer")
    return value


def _string_field(record: dict[str, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise DomainVersionError(f"{field} must be a non-empty string")
    return value


def _evaluate_evidence(
    current: dict[str, object],
    proposed: dict[str, object],
) -> dict[str, object]:
    current_record_id = _string_field(current, "record_id")
    proposed_record_id = _string_field(proposed, "record_id")
    if proposed_record_id == current_record_id:
        raise DomainVersionError("immutable evidence must create a new record_id")
    if _string_field(proposed, "stable_id") != _string_field(current, "stable_id"):
        raise DomainVersionError("stable_id must remain unchanged")
    if _integer_revision(proposed, "revision") != _integer_revision(current, "revision") + 1:
        raise DomainVersionError("revision must increment by exactly 1")
    if proposed.get("supersedes") != current_record_id:
        raise DomainVersionError("supersedes must reference the current record_id")
    return {
        "classification": "evidence",
        "mode": "immutable_successor",
        "allowed": True,
    }


def _evaluate_working(
    current: dict[str, object],
    proposed: dict[str, object],
) -> dict[str, object]:
    if _string_field(proposed, "record_id") != _string_field(current, "record_id"):
        raise DomainVersionError("working record_id must remain unchanged")
    if _integer_revision(proposed, "revision") <= _integer_revision(current, "revision"):
        raise DomainVersionError("revision must increase")
    return {
        "classification": "working",
        "mode": "revisioned_in_place",
        "allowed": True,
    }


def evaluate_domain_mutation(
    record_type: str,
    *,
    current: dict[str, object],
    proposed: dict[str, object],
) -> dict[str, object]:
    record_policy = _record_policy(record_type)
    if record_policy["domain_write_authority"] is not True:
        raise DomainVersionError(f"{record_type} has no domain write authority")
    classification = str(record_policy["classification"])
    if classification == "evidence":
        return _evaluate_evidence(current, proposed)
    if classification == "working":
        return _evaluate_working(current, proposed)
    raise DomainVersionError(f"unsupported mutation classification: {classification}")
