from __future__ import annotations

from pathlib import Path

import pytest

import opc_ceo.domain_versions as domain_versions
from opc_ceo.domain_versions import (
    DomainVersionError,
    classify_record_type,
    evaluate_domain_mutation,
    load_domain_version_policy,
)


def test_policy_keeps_p0_imported_records_sheet_owned_and_not_domain_writable() -> None:
    policy = load_domain_version_policy()

    pipeline = policy["record_types"]["pipeline"]

    assert pipeline["owner"] == "google_sheets"
    assert pipeline["domain_write_authority"] is False
    assert classify_record_type("pipeline") == "p0_imported"
    with pytest.raises(DomainVersionError, match="domain write authority"):
        evaluate_domain_mutation(
            "pipeline",
            current={"record_id": "pipeline_alpha", "revision": 1},
            proposed={"record_id": "pipeline_alpha", "revision": 2},
        )


def test_evidence_records_require_immutable_successor_revisions() -> None:
    result = evaluate_domain_mutation(
        "quote",
        current={
            "record_id": "quote_acme_r001",
            "stable_id": "quote_acme",
            "revision": 1,
        },
        proposed={
            "record_id": "quote_acme_r002",
            "stable_id": "quote_acme",
            "revision": 2,
            "supersedes": "quote_acme_r001",
        },
    )

    assert result == {
        "classification": "evidence",
        "mode": "immutable_successor",
        "allowed": True,
    }


def test_evidence_records_reject_in_place_overwrite_and_broken_lineage() -> None:
    current = {
        "record_id": "invoice_acme_r001",
        "stable_id": "invoice_acme",
        "revision": 1,
    }
    with pytest.raises(DomainVersionError, match="must create a new record_id"):
        evaluate_domain_mutation(
            "invoice",
            current=current,
            proposed={
                "record_id": "invoice_acme_r001",
                "stable_id": "invoice_acme",
                "revision": 2,
                "supersedes": "invoice_acme_r001",
            },
        )

    with pytest.raises(DomainVersionError, match="supersedes"):
        evaluate_domain_mutation(
            "invoice",
            current=current,
            proposed={
                "record_id": "invoice_acme_r002",
                "stable_id": "invoice_acme",
                "revision": 2,
            },
        )


def test_working_records_allow_revisioned_in_place_updates() -> None:
    result = evaluate_domain_mutation(
        "lead",
        current={"record_id": "lead_acme", "revision": 3},
        proposed={"record_id": "lead_acme", "revision": 4},
    )

    assert result == {
        "classification": "working",
        "mode": "revisioned_in_place",
        "allowed": True,
    }


def test_unknown_record_type_is_rejected() -> None:
    with pytest.raises(DomainVersionError, match="unknown record type"):
        classify_record_type("unknown")


def test_invalid_policy_version_is_rejected(tmp_path: Path) -> None:
    policy = tmp_path / "policy.json"
    policy.write_text('{"version":"2.0.0"}\n', encoding="utf-8")

    with pytest.raises(DomainVersionError, match="unsupported"):
        load_domain_version_policy(policy)


def test_evidence_records_reject_invalid_stable_id_and_revision() -> None:
    current = {
        "record_id": "quote_acme_r001",
        "stable_id": "quote_acme",
        "revision": 1,
    }
    with pytest.raises(DomainVersionError, match="stable_id"):
        evaluate_domain_mutation(
            "quote",
            current=current,
            proposed={
                "record_id": "quote_beta_r002",
                "stable_id": "quote_beta",
                "revision": 2,
                "supersedes": "quote_acme_r001",
            },
        )

    with pytest.raises(DomainVersionError, match="revision"):
        evaluate_domain_mutation(
            "quote",
            current=current,
            proposed={
                "record_id": "quote_acme_r002",
                "stable_id": "quote_acme",
                "revision": 4,
                "supersedes": "quote_acme_r001",
            },
        )


def test_domain_records_reject_bad_field_shapes() -> None:
    with pytest.raises(DomainVersionError, match="record_id"):
        evaluate_domain_mutation(
            "quote",
            current={"record_id": "quote_acme_r001", "stable_id": "quote_acme", "revision": 1},
            proposed={
                "record_id": "",
                "stable_id": "quote_acme",
                "revision": 2,
                "supersedes": "quote_acme_r001",
            },
        )

    with pytest.raises(DomainVersionError, match="revision"):
        evaluate_domain_mutation(
            "lead",
            current={"record_id": "lead_acme", "revision": "3"},
            proposed={"record_id": "lead_acme", "revision": 4},
        )


def test_working_records_reject_id_change_and_non_increasing_revision() -> None:
    with pytest.raises(DomainVersionError, match="record_id"):
        evaluate_domain_mutation(
            "lead",
            current={"record_id": "lead_acme", "revision": 3},
            proposed={"record_id": "lead_beta", "revision": 4},
        )

    with pytest.raises(DomainVersionError, match="revision"):
        evaluate_domain_mutation(
            "lead",
            current={"record_id": "lead_acme", "revision": 3},
            proposed={"record_id": "lead_acme", "revision": 3},
        )


def test_unsupported_mutation_classification_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        domain_versions,
        "_record_policy",
        lambda record_type: {
            "classification": "future",
            "domain_write_authority": True,
        },
    )

    with pytest.raises(DomainVersionError, match="unsupported mutation classification"):
        evaluate_domain_mutation(
            "future",
            current={"record_id": "future_1", "revision": 1},
            proposed={"record_id": "future_1", "revision": 2},
        )
