"""Manager and application-owner certification campaigns."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from iga_lab.errors import CampaignError, LoadError
from iga_lab.models import ProvisioningResult, Warehouse
from iga_lab.publish import Publisher

MANAGER = "manager"
APPLICATION_OWNER = "application_owner"


@dataclass
class CampaignItem:
    item_id: str
    reviewer_id: str
    subject_person_id: str | None
    account_id: str | None
    app_id: str | None
    entitlement_id: str | None
    role_id: str | None
    decision: str = "pending"
    decided_by: str | None = None
    decided_at: str | None = None
    comment: str = ""

    def to_dict(self, warehouse: Warehouse) -> dict[str, object]:
        subject = warehouse.people.get(self.subject_person_id or "")
        reviewer = warehouse.people.get(self.reviewer_id)
        decider = warehouse.people.get(self.decided_by or "")
        role = warehouse.roles.get(self.role_id or "")
        return {
            "item_id": self.item_id,
            "decision": self.decision,
            "reviewer_id": self.reviewer_id,
            "reviewer_name": reviewer.display_name if reviewer else None,
            "decided_by": self.decided_by,
            "decided_by_name": decider.display_name if decider else None,
            "decided_at": self.decided_at,
            "comment": self.comment,
            "subject_person_id": self.subject_person_id,
            "subject_name": subject.display_name if subject else None,
            "account_id": self.account_id,
            "app_id": self.app_id,
            "entitlement_id": self.entitlement_id,
            "role_id": self.role_id,
            "role_name": role.name if role else None,
        }


@dataclass
class Campaign:
    campaign_id: str
    name: str
    kind: str
    status: str
    opened_at: str
    due_at: str
    closed_at: str | None
    app_id: str | None
    items: list[CampaignItem] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        counts = {"approve": 0, "revoke": 0, "missed": 0, "pending": 0}
        for item in self.items:
            counts[item.decision] = counts.get(item.decision, 0) + 1
        return counts

    def to_dict(self, warehouse: Warehouse) -> dict[str, object]:
        return {
            "campaign_id": self.campaign_id,
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "opened_at": self.opened_at,
            "due_at": self.due_at,
            "closed_at": self.closed_at,
            "app_id": self.app_id,
            "counts": self.counts(),
            "items": [item.to_dict(warehouse) for item in self.items],
        }


@dataclass
class DecisionOverride:
    decision: str
    reviewer_id: str
    comment: str
    person_id: str | None = None
    role_id: str | None = None
    account_id: str | None = None
    entitlement_id: str | None = None


@dataclass
class CampaignSpec:
    campaign_id: str
    name: str
    kind: str
    due_at: str
    default_decision: str
    overrides: list[DecisionOverride]
    unreviewed: list[dict[str, str]]
    app_id: str | None = None
    fallback_reviewer_id: str | None = None
    fallback_alternate_id: str | None = None


def load_campaign_spec(path: Path) -> CampaignSpec:
    """Load one campaign decision fixture."""
    if not path.exists():
        raise LoadError(f"Missing campaign file {path}.")
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise LoadError(f"{path} must contain a mapping.")
    kind = _require(payload, "kind", path)
    if kind not in {MANAGER, APPLICATION_OWNER}:
        raise LoadError(f"{path} has unsupported kind {kind}.")
    default_decision = str(payload.get("default_decision") or "approve")
    if default_decision != "approve":
        raise LoadError(f"{path} default_decision must be approve.")
    overrides = [_override(row, path) for row in _rows(payload, "overrides", path)]
    unreviewed = []
    for row in _rows(payload, "unreviewed", path):
        if not isinstance(row, dict):
            raise LoadError(f"{path} unreviewed entries must be mappings.")
        unreviewed.append({str(key): str(value) for key, value in row.items() if value is not None})
    return CampaignSpec(
        campaign_id=_require(payload, "campaign_id", path),
        name=_require(payload, "name", path),
        kind=kind,
        due_at=_require(payload, "due_at", path),
        default_decision=default_decision,
        overrides=overrides,
        unreviewed=unreviewed,
        app_id=_optional(payload.get("app_id")),
        fallback_reviewer_id=_optional(payload.get("fallback_reviewer_id")),
        fallback_alternate_id=_optional(payload.get("fallback_alternate_id")),
    )


def open_campaign(warehouse: Warehouse, spec: CampaignSpec, clock: object) -> Campaign:
    """Build review items from current access and record decisions that were supplied."""
    opened_at = clock.iso()  # type: ignore[attr-defined]
    if spec.kind == MANAGER:
        items = _manager_items(warehouse, spec)
    else:
        items = _owner_items(warehouse, spec)
    campaign = Campaign(
        campaign_id=spec.campaign_id,
        name=spec.name,
        kind=spec.kind,
        status="open",
        opened_at=opened_at,
        due_at=spec.due_at,
        closed_at=None,
        app_id=spec.app_id,
        items=items,
    )
    by_id = {item.item_id: item for item in items}
    unreviewed_ids = {_match_item(by_id, row, spec.kind) for row in spec.unreviewed}
    override_ids: dict[str, DecisionOverride] = {}
    for override in spec.overrides:
        item_id = _match_override(by_id, override, spec.kind)
        if item_id in unreviewed_ids:
            raise CampaignError(f"{item_id} is both decided and listed as unreviewed.")
        if item_id in override_ids:
            raise CampaignError(f"Duplicate decision for {item_id}.")
        override_ids[item_id] = override
    for item in items:
        if item.item_id in unreviewed_ids:
            continue
        if item.item_id in override_ids:
            chosen = override_ids[item.item_id]
            decide(
                campaign,
                item.item_id,
                chosen.decision,
                chosen.reviewer_id,
                chosen.comment,
                clock,
            )
        else:
            decide(
                campaign,
                item.item_id,
                "approve",
                item.reviewer_id,
                "Approved by the assigned reviewer.",
                clock,
            )
    return campaign


def decide(
    campaign: Campaign,
    item_id: str,
    decision: str,
    reviewer_id: str,
    comment: str,
    clock: object,
) -> None:
    """Record one reviewer decision. The reviewer must be the assigned reviewer."""
    if campaign.status != "open":
        raise CampaignError(f"Campaign {campaign.campaign_id} is closed.")
    if decision not in {"approve", "revoke"}:
        raise CampaignError(f"Unsupported decision {decision}.")
    item = next((candidate for candidate in campaign.items if candidate.item_id == item_id), None)
    if item is None:
        raise CampaignError(f"Unknown campaign item {item_id}.")
    if reviewer_id != item.reviewer_id:
        raise CampaignError(
            f"Reviewer {reviewer_id} cannot decide {item_id}. "
            f"Assigned reviewer is {item.reviewer_id}."
        )
    item.decision = decision
    item.decided_by = reviewer_id
    item.decided_at = clock.iso()  # type: ignore[attr-defined]
    item.comment = comment


def close_campaign(
    warehouse: Warehouse, campaign: Campaign, publisher: Publisher
) -> list[ProvisioningResult]:
    """Mark pending items missed, deprovision revokes, and close the campaign."""
    if campaign.status != "open":
        raise CampaignError(f"Campaign {campaign.campaign_id} is closed.")
    for item in campaign.items:
        if item.decision == "pending":
            item.decision = "missed"
            if not item.comment:
                item.comment = "No review recorded before the campaign closed."
        elif item.decision == "revoke":
            _apply_revoke(warehouse, item)
        elif item.decision != "approve":
            raise CampaignError(f"Unsupported decision {item.decision} on {item.item_id}.")
    campaign.status = "closed"
    campaign.closed_at = publisher.clock.iso()  # type: ignore[attr-defined]
    return publisher.publish()


def _apply_revoke(warehouse: Warehouse, item: CampaignItem) -> None:
    if item.role_id:
        before = len(warehouse.assignments)
        warehouse.assignments = [
            assignment
            for assignment in warehouse.assignments
            if not (
                assignment.person_id == item.subject_person_id
                and assignment.role_id == item.role_id
            )
        ]
        if len(warehouse.assignments) == before:
            raise CampaignError(f"Role assignment missing for {item.item_id}.")
        return
    if not item.account_id or not item.entitlement_id:
        raise CampaignError(f"Item {item.item_id} has nothing to revoke.")
    account = warehouse.accounts.get(item.account_id)
    if account is None:
        raise CampaignError(f"Account {item.account_id} does not exist.")
    if account.person_id:
        warehouse.assignments = [
            assignment
            for assignment in warehouse.assignments
            if not _grants(
                warehouse,
                assignment.person_id,
                assignment.role_id,
                account.app_id,
                item.entitlement_id,
                account.person_id,
            )
        ]
        return
    account.entitlements = [
        entitlement for entitlement in account.entitlements if entitlement != item.entitlement_id
    ]
    if not account.entitlements:
        account.status = "disabled"


def _grants(
    warehouse: Warehouse,
    assignment_person: str,
    role_id: str,
    app_id: str,
    entitlement_id: str | None,
    account_person: str,
) -> bool:
    if assignment_person != account_person or entitlement_id is None:
        return False
    role = warehouse.roles[role_id]
    return any(
        grant.app_id == app_id and grant.entitlement_id == entitlement_id for grant in role.grants
    )


def _manager_items(warehouse: Warehouse, spec: CampaignSpec) -> list[CampaignItem]:
    if not spec.fallback_reviewer_id or not spec.fallback_alternate_id:
        raise CampaignError("Manager campaign needs fallback reviewers.")
    for person_id in (spec.fallback_reviewer_id, spec.fallback_alternate_id):
        if person_id not in warehouse.people:
            raise CampaignError(f"Fallback reviewer {person_id} does not exist.")
    items: list[CampaignItem] = []
    seen: set[str] = set()
    assignments = sorted(
        warehouse.assignments,
        key=lambda assignment: (assignment.person_id, assignment.role_id),
    )
    for assignment in assignments:
        person = warehouse.people[assignment.person_id]
        if person.status != "active":
            continue
        item_id = f"mgr:{person.person_id}:{assignment.role_id}"
        if item_id in seen:
            raise CampaignError(f"Duplicate campaign item {item_id}.")
        seen.add(item_id)
        items.append(
            CampaignItem(
                item_id=item_id,
                reviewer_id=_manager_reviewer(person.person_id, person.manager_id, spec),
                subject_person_id=person.person_id,
                account_id=None,
                app_id=None,
                entitlement_id=None,
                role_id=assignment.role_id,
            )
        )
    return items


def _owner_items(warehouse: Warehouse, spec: CampaignSpec) -> list[CampaignItem]:
    if not spec.app_id or spec.app_id not in warehouse.apps:
        raise CampaignError("Application owner campaign needs a known app_id.")
    app = warehouse.apps[spec.app_id]
    items: list[CampaignItem] = []
    seen: set[str] = set()
    for account in sorted(warehouse.accounts.values(), key=lambda item: item.account_id):
        if account.app_id != app.app_id or account.status != "active":
            continue
        for entitlement in account.entitlements:
            item_id = f"app:{account.account_id}:{entitlement}"
            if item_id in seen:
                raise CampaignError(f"Duplicate campaign item {item_id}.")
            seen.add(item_id)
            items.append(
                CampaignItem(
                    item_id=item_id,
                    reviewer_id=app.owner_person_id,
                    subject_person_id=account.person_id,
                    account_id=account.account_id,
                    app_id=account.app_id,
                    entitlement_id=entitlement,
                    role_id=None,
                )
            )
    return items


def _manager_reviewer(person_id: str, manager_id: str | None, spec: CampaignSpec) -> str:
    if manager_id:
        return manager_id
    if person_id == spec.fallback_reviewer_id:
        return str(spec.fallback_alternate_id)
    return str(spec.fallback_reviewer_id)


def _match_item(by_id: dict[str, CampaignItem], row: dict[str, str], kind: str) -> str:
    item_id = _row_item_id(row, kind)
    if item_id not in by_id:
        raise CampaignError(f"Unreviewed item {item_id} is not in the campaign.")
    return item_id


def _match_override(by_id: dict[str, CampaignItem], override: DecisionOverride, kind: str) -> str:
    row = {
        "person_id": override.person_id or "",
        "role_id": override.role_id or "",
        "account_id": override.account_id or "",
        "entitlement_id": override.entitlement_id or "",
    }
    row = {key: value for key, value in row.items() if value}
    item_id = _row_item_id(row, kind)
    if item_id not in by_id:
        raise CampaignError(f"Decision target {item_id} is not in the campaign.")
    return item_id


def _row_item_id(row: dict[str, str], kind: str) -> str:
    if kind == MANAGER:
        person_id = row.get("person_id")
        role_id = row.get("role_id")
        if not person_id or not role_id:
            raise CampaignError("Manager campaign items need person_id and role_id.")
        return f"mgr:{person_id}:{role_id}"
    account_id = row.get("account_id")
    entitlement_id = row.get("entitlement_id")
    if not account_id or not entitlement_id:
        raise CampaignError("Application campaign items need account_id and entitlement_id.")
    return f"app:{account_id}:{entitlement_id}"


def _override(row: Any, path: Path) -> DecisionOverride:
    if not isinstance(row, dict):
        raise LoadError(f"{path} overrides must be mappings.")
    decision = _require(row, "decision", path)
    if decision not in {"approve", "revoke"}:
        raise LoadError(f"{path} has unsupported decision {decision}.")
    return DecisionOverride(
        decision=decision,
        reviewer_id=_require(row, "reviewer_id", path),
        comment=str(row.get("comment") or ""),
        person_id=_optional(row.get("person_id")),
        role_id=_optional(row.get("role_id")),
        account_id=_optional(row.get("account_id")),
        entitlement_id=_optional(row.get("entitlement_id")),
    )


def _rows(payload: dict[str, Any], field: str, path: Path) -> list[Any]:
    value = payload.get(field, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise LoadError(f"{path} field {field} must be a list.")
    return list(value)


def _require(row: dict[str, Any], field: str, path: Path) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise LoadError(f"{path} field {field} is required.")
    return value


def _optional(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise LoadError(f"Expected a string, got {value!r}.")
    return value
