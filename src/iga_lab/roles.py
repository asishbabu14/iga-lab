"""Role catalog, birthright rules, and account projection."""

from __future__ import annotations

from iga_lab.models import Account, RoleAssignment, Warehouse

BIRTHRIGHT = "birthright"
REQUEST = "request"


def birthright_role_ids(warehouse: Warehouse, person_id: str) -> set[str]:
    """Return birthright role ids that apply to this person right now."""
    person = warehouse.people[person_id]
    if person.status != "active":
        return set()
    matched: set[str] = set()
    for role in warehouse.roles.values():
        if role.kind != BIRTHRIGHT:
            continue
        if role.population == "all_active":
            matched.add(role.role_id)
        elif role.population == "department" and person.department == role.department:
            matched.add(role.role_id)
    return matched


def sync_birthright(warehouse: Warehouse, granted_at: str) -> list[str]:
    """Add missing birthright roles and remove birthright roles that no longer apply.

    Requestable assignments are left alone. A later HR sync can grant a birthright
    role again after a certification removed it, because birthright follows the
    HR population rule.
    """
    notes: list[str] = []
    for person in sorted(warehouse.people.values(), key=lambda item: item.person_id):
        desired = birthright_role_ids(warehouse, person.person_id)
        current = [
            assignment
            for assignment in warehouse.assignments
            if assignment.person_id == person.person_id and assignment.source == BIRTHRIGHT
        ]
        current_ids = {assignment.role_id for assignment in current}
        for role_id in sorted(desired - current_ids):
            warehouse.assignments.append(
                RoleAssignment(
                    person_id=person.person_id,
                    role_id=role_id,
                    source=BIRTHRIGHT,
                    granted_at=granted_at,
                )
            )
            notes.append(f"grant {person.person_id} {role_id}")
        for assignment in current:
            if assignment.role_id not in desired:
                warehouse.assignments.remove(assignment)
                notes.append(f"revoke {person.person_id} {assignment.role_id}")
    return notes


def clear_assignments(warehouse: Warehouse, person_id: str) -> list[str]:
    """Remove every role assignment for a person. Used by leaver processing."""
    notes: list[str] = []
    kept = []
    for assignment in warehouse.assignments:
        if assignment.person_id == person_id:
            notes.append(f"revoke {person_id} {assignment.role_id}")
        else:
            kept.append(assignment)
    warehouse.assignments = kept
    return notes


def project_role_accounts(warehouse: Warehouse) -> None:
    """Rebuild role-backed accounts from current assignments.

    Orphan accounts are not changed. Linked legacy accounts of a terminated
    person are disabled.
    """
    desired: dict[tuple[str, str], set[str]] = {}
    for assignment in warehouse.assignments:
        person = warehouse.people[assignment.person_id]
        if person.status != "active":
            continue
        role = warehouse.roles[assignment.role_id]
        for grant in role.grants:
            desired.setdefault((person.person_id, grant.app_id), set()).add(grant.entitlement_id)

    for (person_id, app_id), entitlements in desired.items():
        person = warehouse.people[person_id]
        account = warehouse.role_account(person_id, app_id)
        if account is None:
            account = Account(
                account_id=f"acct-{app_id}-{person_id}",
                app_id=app_id,
                person_id=person_id,
                username=person.uid,
                status="active",
                entitlements=sorted(entitlements),
                origin="role",
            )
            warehouse.accounts[account.account_id] = account
        else:
            account.username = person.uid
            account.status = "active"
            account.entitlements = sorted(entitlements)

    for account in warehouse.accounts.values():
        if account.origin != "role":
            if account.person_id and warehouse.people[account.person_id].status != "active":
                account.status = "disabled"
                account.entitlements = []
            continue
        if account.person_id is None:
            continue
        if (account.person_id, account.app_id) not in desired:
            account.status = "disabled"
            account.entitlements = []
