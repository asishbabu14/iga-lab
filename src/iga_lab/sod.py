"""Separation-of-duties evaluation and the exception path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iga_lab.clock import parse_iso
from iga_lab.errors import LoadError
from iga_lab.models import Warehouse


@dataclass
class SodPolicy:
    policy_id: str
    name: str
    description: str
    severity: str
    left_app: str
    left_entitlement: str
    right_app: str
    right_entitlement: str

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "name": self.name,
            "description": self.description,
            "severity": self.severity,
            "left": {"app_id": self.left_app, "entitlement_id": self.left_entitlement},
            "right": {"app_id": self.right_app, "entitlement_id": self.right_entitlement},
        }


@dataclass
class SodException:
    exception_id: str
    policy_id: str
    person_id: str
    approver_id: str
    reason: str
    expires_at: str
    status: str


@dataclass
class SodViolation:
    policy_id: str
    policy_name: str
    person_id: str
    severity: str
    left_app: str
    left_entitlement: str
    right_app: str
    right_entitlement: str
    status: str
    exception_id: str | None

    def to_dict(self, warehouse: Warehouse) -> dict[str, object]:
        person = warehouse.people.get(self.person_id)
        return {
            "policy_id": self.policy_id,
            "policy_name": self.policy_name,
            "person_id": self.person_id,
            "person_name": person.display_name if person else None,
            "severity": self.severity,
            "status": self.status,
            "exception_id": self.exception_id,
            "left": {"app_id": self.left_app, "entitlement_id": self.left_entitlement},
            "right": {"app_id": self.right_app, "entitlement_id": self.right_entitlement},
        }


@dataclass
class ExceptionAssessment:
    exception_id: str
    policy_id: str
    person_id: str
    approver_id: str
    reason: str
    expires_at: str
    recorded_status: str
    outcome: str

    def to_dict(self, warehouse: Warehouse) -> dict[str, object]:
        person = warehouse.people.get(self.person_id)
        approver = warehouse.people.get(self.approver_id)
        return {
            "exception_id": self.exception_id,
            "policy_id": self.policy_id,
            "person_id": self.person_id,
            "person_name": person.display_name if person else None,
            "approver_id": self.approver_id,
            "approver_name": approver.display_name if approver else None,
            "reason": self.reason,
            "expires_at": self.expires_at,
            "recorded_status": self.recorded_status,
            "outcome": self.outcome,
        }


@dataclass
class SodReport:
    evaluated_at: str
    violations: list[SodViolation]
    assessments: list[ExceptionAssessment]

    def to_dict(self, warehouse: Warehouse) -> dict[str, object]:
        return {
            "evaluated_at": self.evaluated_at,
            "violations": [item.to_dict(warehouse) for item in self.violations],
            "exception_assessments": [item.to_dict(warehouse) for item in self.assessments],
        }


def load_policies(rows: list[dict[str, Any]], warehouse: Warehouse) -> list[SodPolicy]:
    """Validate SoD policies against the application catalog."""
    policies: list[SodPolicy] = []
    seen: set[str] = set()
    for row in rows:
        policy = SodPolicy(
            policy_id=_require(row, "policy_id"),
            name=_require(row, "name"),
            description=str(row.get("description") or ""),
            severity=_require(row, "severity"),
            left_app=_side(row, "left", "app_id"),
            left_entitlement=_side(row, "left", "entitlement_id"),
            right_app=_side(row, "right", "app_id"),
            right_entitlement=_side(row, "right", "entitlement_id"),
        )
        if policy.policy_id in seen:
            raise LoadError(f"Duplicate SoD policy {policy.policy_id}.")
        seen.add(policy.policy_id)
        _check_side(warehouse, policy.policy_id, policy.left_app, policy.left_entitlement)
        _check_side(warehouse, policy.policy_id, policy.right_app, policy.right_entitlement)
        if (policy.left_app, policy.left_entitlement) == (
            policy.right_app,
            policy.right_entitlement,
        ):
            raise LoadError(f"{policy.policy_id} compares an entitlement with itself.")
        policies.append(policy)
    return policies


def load_exceptions(
    rows: list[dict[str, Any]], policies: list[SodPolicy], warehouse: Warehouse
) -> list[SodException]:
    """Validate exception records. Expiry is interpreted later, at evaluation time."""
    known = {policy.policy_id for policy in policies}
    exceptions: list[SodException] = []
    seen: set[str] = set()
    for row in rows:
        exception = SodException(
            exception_id=_require(row, "exception_id"),
            policy_id=_require(row, "policy_id"),
            person_id=_require(row, "person_id"),
            approver_id=_require(row, "approver_id"),
            reason=_require(row, "reason"),
            expires_at=_require(row, "expires_at"),
            status=_require(row, "status"),
        )
        if exception.exception_id in seen:
            raise LoadError(f"Duplicate SoD exception {exception.exception_id}.")
        seen.add(exception.exception_id)
        if exception.policy_id not in known:
            raise LoadError(f"{exception.exception_id} references unknown policy.")
        if exception.person_id not in warehouse.people:
            raise LoadError(f"{exception.exception_id} references unknown person.")
        if exception.approver_id not in warehouse.people:
            raise LoadError(f"{exception.exception_id} references unknown approver.")
        if exception.status not in {"active", "inactive"}:
            raise LoadError(f"{exception.exception_id} status must be active or inactive.")
        parse_iso(exception.expires_at)
        exceptions.append(exception)
    return exceptions


def evaluate(
    warehouse: Warehouse,
    policies: list[SodPolicy],
    exceptions: list[SodException],
    clock: object,
) -> SodReport:
    """Compare effective access with policy. Exceptions suppress only when still valid."""
    access = effective_entitlements(warehouse)
    found: list[tuple[SodPolicy, str]] = []
    for policy in policies:
        left = (policy.left_app, policy.left_entitlement)
        right = (policy.right_app, policy.right_entitlement)
        for person_id, entitlements in sorted(access.items()):
            if left in entitlements and right in entitlements:
                found.append((policy, person_id))
    violating = {(policy.policy_id, person_id) for policy, person_id in found}
    assessments: list[ExceptionAssessment] = []
    applied: dict[tuple[str, str], str] = {}
    for exception in exceptions:
        outcome = _outcome(exception, violating, clock)
        assessments.append(
            ExceptionAssessment(
                exception_id=exception.exception_id,
                policy_id=exception.policy_id,
                person_id=exception.person_id,
                approver_id=exception.approver_id,
                reason=exception.reason,
                expires_at=exception.expires_at,
                recorded_status=exception.status,
                outcome=outcome,
            )
        )
        if outcome == "applied" and (exception.policy_id, exception.person_id) not in applied:
            applied[(exception.policy_id, exception.person_id)] = exception.exception_id
    violations = []
    for policy, person_id in found:
        key = (policy.policy_id, person_id)
        violations.append(
            SodViolation(
                policy_id=policy.policy_id,
                policy_name=policy.name,
                person_id=person_id,
                severity=policy.severity,
                left_app=policy.left_app,
                left_entitlement=policy.left_entitlement,
                right_app=policy.right_app,
                right_entitlement=policy.right_entitlement,
                status="excepted" if key in applied else "open",
                exception_id=applied.get(key),
            )
        )
    return SodReport(
        evaluated_at=clock.iso(),  # type: ignore[attr-defined]
        violations=violations,
        assessments=assessments,
    )


def effective_entitlements(warehouse: Warehouse) -> dict[str, set[tuple[str, str]]]:
    """Union of active account entitlements and entitlements implied by roles."""
    access: dict[str, set[tuple[str, str]]] = {}
    for person in warehouse.people.values():
        if person.status != "active":
            continue
        access.setdefault(person.person_id, set())
    for account in warehouse.accounts.values():
        if account.status != "active" or not account.person_id:
            continue
        if warehouse.people[account.person_id].status != "active":
            continue
        bucket = access.setdefault(account.person_id, set())
        for entitlement in account.entitlements:
            bucket.add((account.app_id, entitlement))
    for assignment in warehouse.assignments:
        person = warehouse.people[assignment.person_id]
        if person.status != "active":
            continue
        role = warehouse.roles[assignment.role_id]
        bucket = access.setdefault(person.person_id, set())
        for grant in role.grants:
            bucket.add((grant.app_id, grant.entitlement_id))
    return access


def _outcome(exception: SodException, violating: set[tuple[str, str]], clock: object) -> str:
    """Decide whether an exception record suppresses a violation.

    Applied only when the record is active, the person currently holds both
    sides of the policy, and expiry is still in the future. An expired record
    is reported and does not suppress the finding.
    """
    if exception.status != "active":
        return "inactive"
    if parse_iso(exception.expires_at) <= clock.now():  # type: ignore[attr-defined]
        return "expired"
    if (exception.policy_id, exception.person_id) not in violating:
        return "no_violation"
    return "applied"


def _check_side(warehouse: Warehouse, policy_id: str, app_id: str, entitlement_id: str) -> None:
    app = warehouse.apps.get(app_id)
    if app is None:
        raise LoadError(f"{policy_id} references unknown app {app_id}.")
    if entitlement_id not in app.entitlements:
        raise LoadError(f"{policy_id} references unknown entitlement {entitlement_id}.")


def _side(row: dict[str, Any], name: str, field: str) -> str:
    side = row.get(name)
    if not isinstance(side, dict):
        raise LoadError(f"Policy {row.get('policy_id')} needs a {name} mapping.")
    value = side.get(field)
    if not isinstance(value, str) or not value:
        raise LoadError(f"Policy {row.get('policy_id')} {name}.{field} is required.")
    return value


def _require(row: dict[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise LoadError(f"SoD field {field} is required.")
    return value
