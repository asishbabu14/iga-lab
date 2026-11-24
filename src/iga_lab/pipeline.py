"""Run the lab from seed files through the evidence pack."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from iga_lab.campaigns import Campaign, close_campaign, load_campaign_spec, open_campaign
from iga_lab.clock import ManualClock
from iga_lab.connectors import ConnectorRouter, FileConnector, ScimConnector
from iga_lab.connectors.scim import ScimTransport
from iga_lab.evidence import build_evidence, write_evidence
from iga_lab.jml import apply_feed
from iga_lab.load import (
    APP_OWNER_CAMPAIGN_FILE,
    HR_EVENTS_FILE,
    MANAGER_CAMPAIGN_FILE,
    load_hr_events,
    load_sod,
    load_warehouse,
)
from iga_lab.models import JmlResult, ProvisioningResult, Warehouse
from iga_lab.publish import Publisher
from iga_lab.roles import sync_birthright
from iga_lab.sod import SodPolicy, SodReport, evaluate, load_exceptions, load_policies

LINEAGE = [
    "data/people.yaml",
    "data/apps.yaml",
    "data/roles.yaml",
    "data/assignments.yaml",
    "data/accounts.yaml",
    "data/hr_events.yaml",
    "data/sod_policies.yaml",
    "data/sod_exceptions.yaml",
    "data/campaigns/manager_2026q1.yaml",
    "data/campaigns/app_owner_wiki_2026q1.yaml",
]


@dataclass
class PipelineConfig:
    data_dir: Path
    report_dir: Path
    file_root: Path
    clock: ManualClock
    scim_client: ScimTransport


@dataclass
class PipelineResult:
    warehouse: Warehouse
    evidence: dict[str, object]
    report_paths: dict[str, Path]
    birthright_notes: list[str]
    initial_results: list[ProvisioningResult]
    jml_results: list[JmlResult]
    sod_before: SodReport
    sod_after: SodReport
    campaigns: list[Campaign]
    policies: list[SodPolicy]
    file_root: Path


def run_pipeline(config: PipelineConfig) -> PipelineResult:
    """Load the lab, run lifecycle and certifications, and write evidence."""
    warehouse = load_warehouse(config.data_dir)
    birthright_notes = sync_birthright(warehouse, config.clock.iso())
    file_connector = FileConnector(config.file_root)
    router = ConnectorRouter(
        {
            "MOCK:scim": ScimConnector(config.scim_client),
            "MOCK:file": file_connector,
        }
    )
    publisher = Publisher(warehouse, router, config.clock)
    initial_results = publisher.publish()

    jml_results = apply_feed(
        warehouse,
        load_hr_events(config.data_dir / HR_EVENTS_FILE),
        publisher,
        config.clock,
    )

    policy_rows, exception_rows = load_sod(config.data_dir)
    policies = load_policies(policy_rows, warehouse)
    exceptions = load_exceptions(exception_rows, policies, warehouse)
    config.clock.advance(minutes=5)
    sod_before = evaluate(warehouse, policies, exceptions, config.clock)

    config.clock.advance(minutes=15)
    manager = open_campaign(
        warehouse,
        load_campaign_spec(config.data_dir / MANAGER_CAMPAIGN_FILE),
        config.clock,
    )
    config.clock.advance(minutes=1)
    close_campaign(warehouse, manager, publisher)

    config.clock.advance(minutes=10)
    app_campaign = open_campaign(
        warehouse,
        load_campaign_spec(config.data_dir / APP_OWNER_CAMPAIGN_FILE),
        config.clock,
    )
    config.clock.advance(minutes=1)
    close_campaign(warehouse, app_campaign, publisher)

    config.clock.advance(minutes=5)
    sod_after = evaluate(warehouse, policies, exceptions, config.clock)
    evidence = build_evidence(
        warehouse=warehouse,
        generated_at=config.clock.iso(),
        birthright_notes=birthright_notes,
        jml_results=jml_results,
        campaigns=[manager, app_campaign],
        policies=policies,
        sod_before=sod_before,
        sod_after=sod_after,
        lineage=LINEAGE,
    )
    paths = write_evidence(evidence, config.report_dir)
    return PipelineResult(
        warehouse=warehouse,
        evidence=evidence,
        report_paths=paths,
        birthright_notes=birthright_notes,
        initial_results=initial_results,
        jml_results=jml_results,
        sod_before=sod_before,
        sod_after=sod_after,
        campaigns=[manager, app_campaign],
        policies=policies,
        file_root=config.file_root,
    )
