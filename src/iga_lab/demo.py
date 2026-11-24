"""Terminal walkthrough used by `make demo`."""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import httpx

from iga_lab.clock import LAB_CLOCK_START, ManualClock
from iga_lab.pipeline import PipelineConfig, PipelineResult, run_pipeline
from iga_lab.scim_server import ScimServer

SECTION_PAUSE = 5.0


def repo_root() -> Path:
    """Find the repository that holds the seed data."""
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / "data" / "people.yaml").exists() and (
            candidate / "pyproject.toml"
        ).exists():
            return candidate
    raise RuntimeError("Could not locate the iga-lab repository root.")


def main(
    argv: list[str] | None = None, *, root: Path | None = None, data_dir: Path | None = None
) -> int:
    """Run the demo. Returns 0 when every provisioning result succeeded."""
    del argv
    root = root or repo_root()
    data_dir = data_dir or (repo_root() / "data")
    report_dir = root / "reports"
    file_root = root / "var" / "filedrop"
    if report_dir.exists():
        shutil.rmtree(report_dir)
    var_dir = root / "var"
    if var_dir.exists():
        shutil.rmtree(var_dir)
    server = ScimServer()
    client: httpx.Client | None = None
    try:
        server.start()
        client = server.client()
        result = run_pipeline(
            PipelineConfig(
                data_dir=data_dir,
                report_dir=report_dir,
                file_root=file_root,
                clock=ManualClock(LAB_CLOCK_START),
                scim_client=client,
            )
        )
        _present(result, client)
    finally:
        if client is not None:
            client.close()
        server.stop()
    failures = result.evidence["summary"]["provisioning_failures"]  # type: ignore[index]
    return 1 if failures else 0


def _present(result: PipelineResult, client: httpx.Client) -> None:
    evidence = result.evidence
    population = evidence["population"]
    say("iga-lab demo")
    say("personal lab | synthetic data | not a commercial IGA product")
    say(f"lab clock {LAB_CLOCK_START.strftime('%Y-%m-%dT%H:%M:%SZ')} (fixed, so evidence repeats)")
    beat(2.0)

    say("")
    say("[1/7] Warehouse after the run")
    say(
        f"  people {population['people']}  employees {population['employees']}  "
        f"contractors {population['contractors']}  terminated {population['terminated']}"
    )
    say(f"  apps {len(evidence['apps'])}")
    for account in evidence["orphan_accounts"]:
        say(
            f"  orphan {account['account_id']}  {account['username']}  "
            f"{account['app_id']}  {account['status']}"
        )
    beat(SECTION_PAUSE)

    say("")
    say("[2/7] Birthright roles and initial provisioning")
    grants = [note for note in result.birthright_notes if note.startswith("grant ")]
    say(f"  birthright grants {len(grants)}")
    _say_connector_counts(result.initial_results)
    beat(SECTION_PAUSE)

    say("")
    say("[3/7] HR feed: joiner, mover, leaver")
    for item in result.jml_results:
        say(f"  {item.summary}")
        say(f"    HR effective {item.effective_at}")
        for note in item.notes:
            say(f"    {note}")
    beat(SECTION_PAUSE)

    say("")
    say("[4/7] Separation of duties before certification")
    say("  Exceptions apply only while active, matching, and unexpired.")
    _say_sod(evidence["sod"]["before_campaigns"], result)
    beat(SECTION_PAUSE)

    say("")
    say("[5/7] Manager certification")
    _say_campaign(result.campaigns[0], result)
    beat(SECTION_PAUSE)

    say("")
    say("[6/7] Application owner certification")
    _say_campaign(result.campaigns[1], result)
    beat(SECTION_PAUSE)

    say("")
    say("[7/7] Evidence pack and target check")
    for label, path in result.report_paths.items():
        say(f"  {label}: {path}")
    cleared = evidence["sod"]["cleared_by_certification"]
    if cleared:
        names = ", ".join(f"{item['person_id']} {item['policy_id']}" for item in cleared)
        say(f"  cleared by certification: {names}")
    say(
        f"  open SoD after reviews: {evidence['sod']['open_after']}  "
        f"provisioning failures: {evidence['summary']['provisioning_failures']}"
    )
    _say_targets(result, client)
    beat(SECTION_PAUSE)
    say("")
    say("demo complete")
    beat(3.0)


def _say_campaign(campaign, result: PipelineResult) -> None:
    counts = campaign.counts()
    say(f"  {campaign.campaign_id}  {campaign.name}")
    say(
        f"  approve {counts.get('approve', 0)}  "
        f"revoke {counts.get('revoke', 0)}  missed {counts.get('missed', 0)}"
    )
    for item in campaign.items:
        if item.decision not in {"revoke", "missed"}:
            continue
        subject = item.subject_person_id or item.account_id
        what = item.role_id or item.entitlement_id
        reviewer = result.warehouse.people[item.reviewer_id].display_name
        decided = item.decided_by or "nobody"
        say(
            f"  {item.decision.upper():6}  {what}  {subject}  "
            f"reviewer {item.reviewer_id} {reviewer}"
        )
        if item.decided_by:
            say(f"         decided_by {decided} at {item.decided_at}")
        else:
            say("         no decision recorded before close")


def _say_sod(report: dict, result: PipelineResult) -> None:
    del result
    for violation in report["violations"]:
        say(
            f"  {violation['status'].upper():9}  {violation['policy_id']}  "
            f"{violation['person_id']} {violation['person_name']}"
        )
    for assessment in report["exception_assessments"]:
        say(
            f"  exception {assessment['exception_id']}  outcome {assessment['outcome']}  "
            f"approver {assessment['approver_id']}"
        )


def _say_connector_counts(results) -> None:
    counts: dict[str, int] = {}
    for item in results:
        counts[item.connector] = counts.get(item.connector, 0) + 1
    for label in sorted(counts):
        say(f"  {label} results {counts[label]}")


def _say_targets(result: PipelineResult, client: httpx.Client) -> None:
    ledger = (result.file_root / "app-ledger.csv").read_text(encoding="utf-8")
    for line in ledger.splitlines():
        if "acct-app-ledger-p-014" in line:
            parts = line.split("|")
            say(f"  MOCK:file {parts[0]} status={parts[3]} entitlements={parts[4]}")
    orphan = client.get("/scim/v2/Users/acct-orphan-wiki")
    if orphan.status_code == 200:
        body = orphan.json()
        say(
            f"  MOCK:scim acct-orphan-wiki active={body.get('active')} "
            f"entitlements={body.get('entitlements')}"
        )
    joiner = client.get("/scim/v2/Users/acct-app-email-p-051")
    if joiner.status_code == 200:
        body = joiner.json()
        say(
            f"  MOCK:scim acct-app-email-p-051 active={body.get('active')} "
            f"entitlements={body.get('entitlements')}"
        )


def say(message: str) -> None:
    print(message, flush=True)


def beat(seconds: float) -> None:
    if _paced():
        time.sleep(seconds)


def _paced() -> bool:
    flag = os.environ.get("IGA_DEMO_PACE", "").strip()
    if flag == "1":
        return True
    if flag == "0":
        return False
    return sys.stdout.isatty()


if __name__ == "__main__":
    raise SystemExit(main())
