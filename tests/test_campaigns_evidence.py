"""Certification decisions, missed reviews, and the evidence pack."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from iga_lab.campaigns import (
    CampaignSpec,
    DecisionOverride,
    close_campaign,
    decide,
    open_campaign,
)
from iga_lab.clock import LAB_CLOCK_START, ManualClock
from iga_lab.demo import main
from iga_lab.errors import CampaignError
from iga_lab.evidence import LAB_NOTICE, build_evidence, render_html
from iga_lab.models import Person, Warehouse
from iga_lab.pipeline import PipelineConfig, run_pipeline
from iga_lab.roles import sync_birthright
from iga_lab.scim_server import create_app
from iga_lab.sod import SodReport
from tests.conftest import ROOT
from tests.support import SyncAsgiClient, assign, make_lab, orphan


def test_approve_revoke_and_missed_review(tmp_path: Path) -> None:
    lab = make_lab(tmp_path)
    assign(lab.warehouse, "p-2", "role-finance-clerk")
    assign(lab.warehouse, "p-2", "role-finance-approver")
    sync_birthright(lab.warehouse, lab.clock.iso())
    lab.publisher.publish()
    spec = CampaignSpec(
        campaign_id="camp-test",
        name="Manager test",
        kind="manager",
        due_at="2026-03-15T00:00:00Z",
        default_decision="approve",
        overrides=[],
        unreviewed=[{"person_id": "p-2", "role_id": "role-finance-clerk"}],
        fallback_reviewer_id="p-1",
        fallback_alternate_id="p-2",
    )
    campaign = open_campaign(lab.warehouse, spec, lab.clock)
    approver = next(
        item for item in campaign.items if item.item_id.endswith("role-finance-approver")
    )
    assert approver.reviewer_id == "p-1"
    assert approver.decision == "approve"
    assert approver.decided_by == "p-1"
    with pytest.raises(CampaignError, match="cannot decide"):
        decide(campaign, approver.item_id, "revoke", "p-2", "wrong person", lab.clock)
    with pytest.raises(CampaignError, match="Unknown"):
        decide(campaign, "mgr:p-2:missing-role", "approve", "p-1", "", lab.clock)
    decide(campaign, approver.item_id, "revoke", "p-1", "Remove approver.", lab.clock)
    missed = next(item for item in campaign.items if item.role_id == "role-finance-clerk")
    assert missed.decision == "pending"
    lab.clock.advance(minutes=1)
    close_campaign(lab.warehouse, campaign, lab.publisher)
    assert missed.decision == "missed"
    assert missed.decided_by is None
    assert any(
        item.person_id == "p-2" and item.role_id == "role-finance-clerk"
        for item in lab.warehouse.assignments
    )
    assert not any(
        item.person_id == "p-2" and item.role_id == "role-finance-approver"
        for item in lab.warehouse.assignments
    )
    ledger = (lab.file_root / "app-ledger.csv").read_text(encoding="utf-8")
    row = [line for line in ledger.splitlines() if "acct-app-ledger-p-2" in line][0]
    assert "journal-approve" not in row
    assert "journal-entry" in row
    assert "MOCK:file" in row
    with pytest.raises(CampaignError, match="closed"):
        decide(campaign, approver.item_id, "approve", "p-1", "too late", lab.clock)


def test_orphan_revoke_disables_scim_user(tmp_path: Path) -> None:
    lab = make_lab(tmp_path)
    orphan(lab.warehouse)
    sync_birthright(lab.warehouse, lab.clock.iso())
    lab.publisher.publish()
    spec = CampaignSpec(
        campaign_id="camp-wiki",
        name="Wiki owner",
        kind="application_owner",
        due_at="2026-03-15T00:00:00Z",
        default_decision="approve",
        app_id="app-wiki",
        overrides=[],
        unreviewed=[],
        fallback_reviewer_id=None,
        fallback_alternate_id=None,
    )
    # Default approve would keep the orphan. Override through decide after open
    # by listing it as a revoke in a second spec.
    spec.overrides = [
        DecisionOverride(
            decision="revoke",
            reviewer_id="p-1",
            comment="Orphan.",
            account_id="acct-orphan-wiki",
            entitlement_id="reader",
        )
    ]
    campaign = open_campaign(lab.warehouse, spec, lab.clock)
    assert any(item.account_id == "acct-orphan-wiki" for item in campaign.items)
    close_campaign(lab.warehouse, campaign, lab.publisher)
    assert lab.warehouse.accounts["acct-orphan-wiki"].status == "disabled"
    body = lab.client.get("/scim/v2/Users/acct-orphan-wiki").json()
    assert body["active"] is False
    assert body["mockTarget"] == "MOCK:scim"


def test_unknown_override_is_rejected(tmp_path: Path) -> None:
    lab = make_lab(tmp_path)
    sync_birthright(lab.warehouse, lab.clock.iso())
    spec = CampaignSpec(
        campaign_id="camp-test",
        name="Manager test",
        kind="manager",
        due_at="2026-03-15T00:00:00Z",
        default_decision="approve",
        overrides=[],
        unreviewed=[{"person_id": "p-9", "role_id": "role-staff-email"}],
        fallback_reviewer_id="p-1",
        fallback_alternate_id="p-2",
    )
    with pytest.raises(CampaignError, match="not in the campaign"):
        open_campaign(lab.warehouse, spec, lab.clock)


def test_html_escapes_untrusted_names() -> None:
    warehouse = Warehouse()
    warehouse.people["p-1"] = Person(
        person_id="p-1",
        display_name="<script>alert(1)</script>",
        person_type="employee",
        status="active",
        department="finance",
        title="Analyst",
        email="p-1@example.test",
        uid="p-1",
        manager_id=None,
        start_date="2024-01-01",
    )
    empty = SodReport(evaluated_at="2026-02-20T15:00:00Z", violations=[], assessments=[])
    evidence = build_evidence(
        warehouse=warehouse,
        generated_at="2026-02-20T16:00:00Z",
        birthright_notes=[],
        jml_results=[],
        campaigns=[],
        policies=[],
        sod_before=empty,
        sod_after=empty,
        lineage=["data/people.yaml"],
    )
    page = render_html(evidence)
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "http://" not in page
    assert "https://" not in page
    assert LAB_NOTICE in evidence["notice"]


def test_full_seed_pipeline(tmp_path: Path) -> None:
    client = SyncAsgiClient(create_app())
    result = run_pipeline(
        PipelineConfig(
            data_dir=ROOT / "data",
            report_dir=tmp_path / "reports",
            file_root=tmp_path / "filedrop",
            clock=ManualClock(LAB_CLOCK_START),
            scim_client=client,
        )
    )
    warehouse = result.warehouse
    assert warehouse.people["p-051"].department == "engineering"
    assert warehouse.people["p-051"].person_type == "contractor"
    assert warehouse.accounts["acct-app-email-p-051"].entitlements == ["mailbox"]
    mover_roles = {item.role_id for item in warehouse.assignments if item.person_id == "p-021"}
    assert warehouse.people["p-021"].department == "finance"
    assert warehouse.people["p-021"].manager_id == "p-002"
    assert "role-eng-wiki-editor" not in mover_roles
    assert "role-finance-ledger-read" in mover_roles
    assert warehouse.people["p-030"].status == "terminated"
    assert warehouse.accounts["acct-app-email-p-030"].status == "disabled"
    assert warehouse.accounts["acct-app-wiki-p-030"].entitlements == []

    before = {
        (item.policy_id, item.person_id, item.status) for item in result.sod_before.violations
    }
    assert ("sod-ledger-maker-checker", "p-014", "open") in before
    assert ("sod-ledger-maker-checker", "p-015", "excepted") in before
    assert ("sod-payroll-admin-processor", "p-009", "open") in before
    outcomes = {item.exception_id: item.outcome for item in result.sod_before.assessments}
    assert outcomes["ex-ledger-old-p014"] == "expired"
    assert outcomes["ex-ledger-close-p015"] == "applied"
    after = {(item.policy_id, item.person_id) for item in result.sod_after.violations}
    assert ("sod-ledger-maker-checker", "p-014") not in after
    assert ("sod-ledger-maker-checker", "p-015") in after
    assert ("sod-payroll-admin-processor", "p-009") in after

    manager = result.campaigns[0]
    items = {item.item_id: item for item in manager.items}
    assert items["mgr:p-014:role-finance-approver"].decision == "revoke"
    assert items["mgr:p-014:role-finance-approver"].decided_by == "p-002"
    assert items["mgr:p-022:role-vault-operator"].decided_by == "p-016"
    missed = items["mgr:p-019:role-wiki-editor"]
    assert missed.decision == "missed"
    assert missed.decided_by is None
    assert missed.reviewer_id == "p-016"
    assert any(
        item.person_id == "p-019" and item.role_id == "role-wiki-editor"
        for item in warehouse.assignments
    )
    assert "journal-approve" not in warehouse.accounts["acct-app-ledger-p-014"].entitlements
    ledger_row = [
        line
        for line in (result.file_root / "app-ledger.csv").read_text(encoding="utf-8").splitlines()
        if "acct-app-ledger-p-014" in line
    ][0]
    assert "journal-approve" not in ledger_row
    assert warehouse.accounts["acct-app-vault-p-022"].status == "disabled"
    vault = client.get("/scim/v2/Users/acct-app-vault-p-022").json()
    assert vault["active"] is False

    owner = result.campaigns[1]
    owner_items = {item.item_id: item for item in owner.items}
    assert owner_items["app:acct-orphan-wiki:reader"].decision == "revoke"
    assert owner_items["app:acct-orphan-wiki:reader"].decided_by == "p-003"
    assert owner_items["app:acct-app-wiki-p-040:reader"].decision == "missed"
    assert owner_items["app:acct-app-wiki-p-038:editor"].decision == "revoke"
    assert warehouse.accounts["acct-orphan-wiki"].status == "disabled"
    assert "reader" in warehouse.accounts["acct-app-wiki-p-040"].entitlements
    assert "editor" not in warehouse.accounts["acct-app-wiki-p-038"].entitlements
    assert "reader" in warehouse.accounts["acct-app-wiki-p-038"].entitlements
    orphan_body = client.get("/scim/v2/Users/acct-orphan-wiki").json()
    assert orphan_body["active"] is False
    wiki = client.get("/scim/v2/Users/acct-app-wiki-p-038").json()
    assert wiki["active"] is True
    assert "editor" not in wiki["entitlements"]

    assert items["mgr:p-001:role-staff-email"].reviewer_id == "p-002"
    assert items["mgr:p-001:role-staff-email"].decided_by == "p-002"
    assert items["mgr:p-002:role-staff-email"].reviewer_id == "p-001"
    assert result.sod_before.evaluated_at < manager.opened_at < manager.closed_at
    assert manager.closed_at < owner.closed_at < result.evidence["generated_at"]
    revokes = [
        item
        for item in warehouse.provisioning_results
        if item.action == "revoke"
        and item.entitlement_id == "journal-approve"
        and item.account_id == "acct-app-ledger-p-014"
    ]
    assert revokes and revokes[0].success and revokes[0].connector == "MOCK:file"
    assert any(
        item.connector == "MOCK:scim"
        and item.action == "revoke"
        and item.account_id == "acct-orphan-wiki"
        and item.success
        for item in warehouse.provisioning_results
    )
    html = result.report_paths["html"].read_text(encoding="utf-8")
    assert "camp-mgr-2026q1" in html
    assert "Not SOX compliance" in html
    assert "ex-ledger-close-p015" in html
    assert result.report_paths["json"].exists()
    assert result.report_paths["markdown"].exists()
    client.close()


def test_demo_main_writes_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IGA_DEMO_PACE", "0")
    code = main(root=tmp_path, data_dir=ROOT / "data")
    assert code == 0
    html = (tmp_path / "reports" / "evidence.html").read_text(encoding="utf-8")
    assert "Access-control evidence" in html
    assert os.environ["IGA_DEMO_PACE"] == "0"


def test_demo_gif_is_a_real_recording() -> None:
    path = ROOT / "docs" / "demo.gif"
    data = path.read_bytes()
    assert data[:6] == b"GIF89a"
    assert len(data) < 3 * 1024 * 1024
    total = 0
    index = 0
    marker = b"\x21\xf9\x04"
    while True:
        found = data.find(marker, index)
        if found < 0:
            break
        total += int.from_bytes(data[found + 4 : found + 6], "little")
        index = found + 6
    seconds = total / 100
    assert 30 <= seconds <= 60
    assert "docs/demo.gif" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_docs_disclaimer_and_forbidden_words() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    lowered = readme.lower()
    assert "personal lab project" in lowered
    assert "synthetic data" in lowered
    assert "not a commercial iga product" in lowered
    assert "not affiliated with any employer" in lowered
    assert "not sox compliance" in lowered
    assert "workforce iam" in architecture.lower()
    assert "ciam" in architecture.lower()
    for text in (readme, architecture):
        assert "\u2014" not in text
        assert "\u2013" not in text
    forbidden = [
        "sail" + "point",
        "savi" + "ynt",
        "ok" + "ta",
        "toy" + "ota",
        "job " + "post" + "ing",
        "post" + "ing",
    ]
    hits: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in {".git", ".venv", "reports", "var", "__pycache__"} for part in path.parts):
            continue
        if path.suffix in {".png", ".gif", ".pyc"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for word in forbidden:
            if word in text:
                hits.append(f"{path}: {word}")
    assert hits == []
