"""Loader checks and separation-of-duties outcomes."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from iga_lab.errors import LoadError
from iga_lab.load import load_hr_events, load_sod, load_warehouse
from iga_lab.models import Account
from iga_lab.sod import SodException, SodPolicy, evaluate, load_exceptions, load_policies
from tests.conftest import ROOT
from tests.support import make_lab


def test_seed_population_and_orphan() -> None:
    warehouse = load_warehouse(ROOT / "data")
    people = list(warehouse.people.values())
    assert len(people) == 50
    assert sum(person.person_type == "employee" for person in people) == 40
    assert sum(person.person_type == "contractor" for person in people) == 10
    assert sum(person.status == "terminated" for person in people) == 2
    assert len(warehouse.apps) == 5
    orphans = [account for account in warehouse.accounts.values() if account.origin == "orphan"]
    assert len(orphans) == 1
    assert orphans[0].person_id is None
    assert orphans[0].username == "orphan.wiki.legacy"
    connectors = {app.connector for app in warehouse.apps.values()}
    assert connectors == {"MOCK:scim", "MOCK:file"}


def test_seed_requestable_conflict_is_loaded() -> None:
    warehouse = load_warehouse(ROOT / "data")
    roles = {
        assignment.role_id
        for assignment in warehouse.assignments
        if assignment.person_id == "p-014"
    }
    assert roles == {"role-finance-clerk", "role-finance-approver"}


def test_manager_cycle_is_rejected(tmp_path: Path) -> None:
    data = _minimal_data(tmp_path)
    people = yaml.safe_load((data / "people.yaml").read_text(encoding="utf-8"))
    people["people"][0]["manager_id"] = "p-2"
    people["people"][1]["manager_id"] = "p-1"
    (data / "people.yaml").write_text(yaml.safe_dump(people), encoding="utf-8")
    with pytest.raises(LoadError, match="cycle"):
        load_warehouse(data)


def test_birthright_assignment_in_file_is_rejected(tmp_path: Path) -> None:
    data = _minimal_data(tmp_path)
    (data / "assignments.yaml").write_text(
        yaml.safe_dump(
            {
                "assignments": [
                    {
                        "person_id": "p-1",
                        "role_id": "role-staff-email",
                        "granted_at": "2026-01-01T00:00:00Z",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(LoadError, match="requestable"):
        load_warehouse(data)


def test_unknown_connector_label_is_rejected(tmp_path: Path) -> None:
    data = _minimal_data(tmp_path)
    apps = yaml.safe_load((data / "apps.yaml").read_text(encoding="utf-8"))
    apps["apps"][0]["connector"] = "MOCK:other"
    (data / "apps.yaml").write_text(yaml.safe_dump(apps), encoding="utf-8")
    with pytest.raises(LoadError, match="connector"):
        load_warehouse(data)


def test_sod_unknown_entitlement_is_rejected(tmp_path: Path) -> None:
    lab = make_lab(tmp_path)
    rows = [
        {
            "policy_id": "sod-bad",
            "name": "Bad",
            "severity": "high",
            "description": "Nope.",
            "left": {"app_id": "app-ledger", "entitlement_id": "journal-entry"},
            "right": {"app_id": "app-ledger", "entitlement_id": "does-not-exist"},
        }
    ]
    with pytest.raises(LoadError, match="unknown entitlement"):
        load_policies(rows, lab.warehouse)


def test_hr_file_duplicate_event_and_missing_joiner_field(tmp_path: Path) -> None:
    path = tmp_path / "hr.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "events": [
                    {"event_id": "e1", "type": "leaver", "effective_at": "2026-01-01T00:00:00Z"},
                    {"event_id": "e1", "type": "leaver", "effective_at": "2026-01-02T00:00:00Z"},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(LoadError, match="Duplicate"):
        load_hr_events(path)
    path.write_text(
        yaml.safe_dump(
            {
                "events": [
                    {
                        "event_id": "e1",
                        "type": "joiner",
                        "effective_at": "2026-01-01T00:00:00Z",
                        "person": {"person_id": "p-9"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(LoadError, match="missing"):
        load_hr_events(path)


def test_seed_sod_files_load() -> None:
    warehouse = load_warehouse(ROOT / "data")
    policies, exceptions = load_sod(ROOT / "data")
    loaded_policies = load_policies(policies, warehouse)
    loaded_exceptions = load_exceptions(exceptions, loaded_policies, warehouse)
    assert {policy.policy_id for policy in loaded_policies} == {
        "sod-ledger-maker-checker",
        "sod-payroll-admin-processor",
    }
    assert {item.exception_id for item in loaded_exceptions} == {
        "ex-ledger-close-p015",
        "ex-ledger-old-p014",
    }


def test_exception_outcomes(tmp_path: Path) -> None:
    lab = make_lab(tmp_path)
    lab.warehouse.accounts["acct-app-ledger-p-2"] = Account(
        account_id="acct-app-ledger-p-2",
        app_id="app-ledger",
        person_id="p-2",
        username="nia.cole",
        status="active",
        entitlements=["journal-entry", "journal-approve"],
        origin="role",
    )
    policy = SodPolicy(
        policy_id="sod-ledger-maker-checker",
        name="Ledger maker-checker",
        description="Maker and checker.",
        severity="high",
        left_app="app-ledger",
        left_entitlement="journal-entry",
        right_app="app-ledger",
        right_entitlement="journal-approve",
    )
    expired = SodException(
        exception_id="old",
        policy_id=policy.policy_id,
        person_id="p-2",
        approver_id="p-1",
        reason="Old window.",
        expires_at="2020-01-01T00:00:00Z",
        status="active",
    )
    report = evaluate(lab.warehouse, [policy], [expired], lab.clock)
    assert report.violations[0].status == "open"
    assert report.assessments[0].outcome == "expired"

    future = SodException(
        exception_id="open",
        policy_id=policy.policy_id,
        person_id="p-2",
        approver_id="p-1",
        reason="Coverage.",
        expires_at="2026-06-30T00:00:00Z",
        status="active",
    )
    other = SodException(
        exception_id="other",
        policy_id=policy.policy_id,
        person_id="p-3",
        approver_id="p-1",
        reason="Wrong person.",
        expires_at="2026-06-30T00:00:00Z",
        status="active",
    )
    inactive = SodException(
        exception_id="off",
        policy_id=policy.policy_id,
        person_id="p-2",
        approver_id="p-1",
        reason="Turned off.",
        expires_at="2026-06-30T00:00:00Z",
        status="inactive",
    )
    report = evaluate(lab.warehouse, [policy], [future, other, inactive], lab.clock)
    assert report.violations[0].status == "excepted"
    assert report.violations[0].exception_id == "open"
    outcomes = {item.exception_id: item.outcome for item in report.assessments}
    assert outcomes == {"open": "applied", "other": "no_violation", "off": "inactive"}


def _minimal_data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    (data / "people.yaml").write_text(
        yaml.safe_dump(
            {
                "people": [
                    _row("p-1", None),
                    _row("p-2", "p-1"),
                ]
            }
        ),
        encoding="utf-8",
    )
    (data / "apps.yaml").write_text(
        yaml.safe_dump(
            {
                "apps": [
                    {
                        "app_id": "app-email",
                        "name": "Staff email",
                        "owner_person_id": "p-1",
                        "connector": "MOCK:scim",
                        "entitlements": ["mailbox"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (data / "roles.yaml").write_text(
        yaml.safe_dump(
            {
                "roles": [
                    {
                        "role_id": "role-staff-email",
                        "name": "Staff email",
                        "kind": "birthright",
                        "population": "all_active",
                        "description": "Mailbox.",
                        "grants": [{"app_id": "app-email", "entitlement_id": "mailbox"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (data / "assignments.yaml").write_text(
        yaml.safe_dump({"assignments": []}),
        encoding="utf-8",
    )
    (data / "accounts.yaml").write_text(
        yaml.safe_dump({"accounts": []}),
        encoding="utf-8",
    )
    return data


def _row(person_id: str, manager: str | None) -> dict[str, object]:
    return {
        "person_id": person_id,
        "display_name": f"Person {person_id}",
        "person_type": "employee",
        "status": "active",
        "department": "finance",
        "title": "Analyst",
        "email": f"{person_id}@example.test",
        "uid": person_id,
        "manager_id": manager,
        "start_date": "2024-01-01",
        "end_date": None,
    }
