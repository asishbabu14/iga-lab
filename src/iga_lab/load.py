"""Load seed YAML into a warehouse and related policy objects."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from iga_lab.errors import LoadError
from iga_lab.models import (
    Account,
    App,
    Grant,
    HrEvent,
    Person,
    Role,
    RoleAssignment,
    Warehouse,
)

PEOPLE_FILE = "people.yaml"
APPS_FILE = "apps.yaml"
ROLES_FILE = "roles.yaml"
ASSIGNMENTS_FILE = "assignments.yaml"
ACCOUNTS_FILE = "accounts.yaml"
HR_EVENTS_FILE = "hr_events.yaml"
SOD_POLICIES_FILE = "sod_policies.yaml"
SOD_EXCEPTIONS_FILE = "sod_exceptions.yaml"
MANAGER_CAMPAIGN_FILE = "campaigns/manager_2026q1.yaml"
APP_OWNER_CAMPAIGN_FILE = "campaigns/app_owner_wiki_2026q1.yaml"

PERSON_TYPES = {"employee", "contractor"}
PERSON_STATUSES = {"active", "terminated"}
ROLE_KINDS = {"birthright", "requestable"}
CONNECTORS = {"MOCK:scim", "MOCK:file"}
ACCOUNT_ORIGINS = {"orphan", "legacy"}
MOVER_FIELDS = {"department", "title", "manager_id", "email", "person_type"}
JOINER_FIELDS = {
    "person_id",
    "display_name",
    "person_type",
    "department",
    "title",
    "email",
    "uid",
    "manager_id",
    "start_date",
}


def load_warehouse(data_dir: Path) -> Warehouse:
    """Load people, apps, roles, requestable assignments, and pinned accounts."""
    warehouse = Warehouse()
    _load_people(warehouse, _read_mapping(data_dir / PEOPLE_FILE))
    _load_apps(warehouse, _read_mapping(data_dir / APPS_FILE))
    _load_roles(warehouse, _read_mapping(data_dir / ROLES_FILE))
    _load_assignments(warehouse, _read_mapping(data_dir / ASSIGNMENTS_FILE))
    _load_accounts(warehouse, _read_mapping(data_dir / ACCOUNTS_FILE))
    _validate_graph(warehouse)
    return warehouse


def load_hr_events(path: Path) -> list[HrEvent]:
    """Load the thin HR feed. This is not an approval workflow."""
    payload = _read_mapping(path)
    rows = payload.get("events")
    if not isinstance(rows, list):
        raise LoadError(f"{path} needs an events list.")
    events: list[HrEvent] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise LoadError(f"{path} event {index} must be a mapping.")
        event_id = _require_str(row, "event_id", path)
        if event_id in seen:
            raise LoadError(f"Duplicate HR event id {event_id}.")
        seen.add(event_id)
        event_type = _require_str(row, "type", path)
        if event_type not in {"joiner", "mover", "leaver"}:
            raise LoadError(f"Unsupported HR event type {event_type}.")
        effective_at = _require_str(row, "effective_at", path)
        person = row.get("person")
        changes = row.get("changes")
        if event_type == "joiner":
            if not isinstance(person, dict):
                raise LoadError(f"Joiner {event_id} needs a person mapping.")
            _validate_joiner_person(person, event_id)
        if event_type == "mover":
            if not isinstance(changes, dict) or not changes:
                raise LoadError(f"Mover {event_id} needs a non-empty changes mapping.")
            unknown = set(changes) - MOVER_FIELDS
            if unknown:
                raise LoadError(f"Mover {event_id} has unsupported fields {sorted(unknown)}.")
        events.append(
            HrEvent(
                event_id=event_id,
                event_type=event_type,
                effective_at=effective_at,
                person_id=_optional_str(row.get("person_id")),
                person=dict(person) if isinstance(person, dict) else None,
                changes=dict(changes) if isinstance(changes, dict) else None,
                end_date=_optional_str(row.get("end_date")),
            )
        )
    return events


def load_sod(data_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load SoD policies and exceptions. Returned dicts are already validated."""
    policies_payload = _read_mapping(data_dir / SOD_POLICIES_FILE)
    exceptions_payload = _read_mapping(data_dir / SOD_EXCEPTIONS_FILE)
    policies = policies_payload.get("policies")
    exceptions = exceptions_payload.get("exceptions")
    if not isinstance(policies, list) or not policies:
        raise LoadError("sod_policies.yaml needs a non-empty policies list.")
    if not isinstance(exceptions, list):
        raise LoadError("sod_exceptions.yaml needs an exceptions list.")
    return [dict(item) for item in policies], [dict(item) for item in exceptions]


def _load_people(warehouse: Warehouse, payload: dict[str, Any]) -> None:
    rows = payload.get("people")
    if not isinstance(rows, list) or not rows:
        raise LoadError("people.yaml needs a non-empty people list.")
    emails: set[str] = set()
    uids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise LoadError("Each person must be a mapping.")
        person = Person(
            person_id=_require_str(row, "person_id", "people.yaml"),
            display_name=_require_str(row, "display_name", "people.yaml"),
            person_type=_require_str(row, "person_type", "people.yaml"),
            status=_require_str(row, "status", "people.yaml"),
            department=_require_str(row, "department", "people.yaml"),
            title=_require_str(row, "title", "people.yaml"),
            email=_require_str(row, "email", "people.yaml"),
            uid=_require_str(row, "uid", "people.yaml"),
            manager_id=_optional_str(row.get("manager_id")),
            start_date=_require_str(row, "start_date", "people.yaml"),
            end_date=_optional_str(row.get("end_date")),
        )
        if person.person_type not in PERSON_TYPES:
            raise LoadError(f"{person.person_id} has unsupported person_type.")
        if person.status not in PERSON_STATUSES:
            raise LoadError(f"{person.person_id} has unsupported status.")
        if "@" not in person.email:
            raise LoadError(f"{person.person_id} needs an email address.")
        if person.person_id in warehouse.people:
            raise LoadError(f"Duplicate person id {person.person_id}.")
        if person.email in emails:
            raise LoadError(f"Duplicate email {person.email}.")
        if person.uid in uids:
            raise LoadError(f"Duplicate uid {person.uid}.")
        if person.status == "terminated" and not person.end_date:
            raise LoadError(f"Terminated person {person.person_id} needs end_date.")
        if person.status == "active" and person.end_date:
            raise LoadError(f"Active person {person.person_id} cannot have end_date.")
        emails.add(person.email)
        uids.add(person.uid)
        warehouse.people[person.person_id] = person


def _load_apps(warehouse: Warehouse, payload: dict[str, Any]) -> None:
    rows = payload.get("apps")
    if not isinstance(rows, list) or not rows:
        raise LoadError("apps.yaml needs a non-empty apps list.")
    for row in rows:
        if not isinstance(row, dict):
            raise LoadError("Each app must be a mapping.")
        entitlements = row.get("entitlements")
        if not isinstance(entitlements, list) or not entitlements:
            raise LoadError(f"App {row.get('app_id')} needs entitlements.")
        if len(set(entitlements)) != len(entitlements):
            raise LoadError(f"App {row.get('app_id')} has duplicate entitlements.")
        app = App(
            app_id=_require_str(row, "app_id", "apps.yaml"),
            name=_require_str(row, "name", "apps.yaml"),
            owner_person_id=_require_str(row, "owner_person_id", "apps.yaml"),
            connector=_require_str(row, "connector", "apps.yaml"),
            entitlements=[str(item) for item in entitlements],
            description=str(row.get("description") or ""),
        )
        if app.connector not in CONNECTORS:
            raise LoadError(f"{app.app_id} has unsupported connector {app.connector}.")
        if app.app_id in warehouse.apps:
            raise LoadError(f"Duplicate app id {app.app_id}.")
        warehouse.apps[app.app_id] = app


def _load_roles(warehouse: Warehouse, payload: dict[str, Any]) -> None:
    rows = payload.get("roles")
    if not isinstance(rows, list) or not rows:
        raise LoadError("roles.yaml needs a non-empty roles list.")
    for row in rows:
        if not isinstance(row, dict):
            raise LoadError("Each role must be a mapping.")
        grants_raw = row.get("grants")
        if not isinstance(grants_raw, list) or not grants_raw:
            raise LoadError(f"Role {row.get('role_id')} needs grants.")
        grants: list[Grant] = []
        for grant in grants_raw:
            if not isinstance(grant, dict):
                raise LoadError(f"Role {row.get('role_id')} has a bad grant.")
            grants.append(
                Grant(
                    app_id=_require_str(grant, "app_id", "roles.yaml"),
                    entitlement_id=_require_str(grant, "entitlement_id", "roles.yaml"),
                )
            )
        role = Role(
            role_id=_require_str(row, "role_id", "roles.yaml"),
            name=_require_str(row, "name", "roles.yaml"),
            kind=_require_str(row, "kind", "roles.yaml"),
            description=str(row.get("description") or ""),
            grants=grants,
            population=_optional_str(row.get("population")),
            department=_optional_str(row.get("department")),
        )
        if role.kind not in ROLE_KINDS:
            raise LoadError(f"{role.role_id} has unsupported kind.")
        if role.kind == "birthright":
            if role.population not in {"all_active", "department"}:
                raise LoadError(f"{role.role_id} needs population all_active or department.")
            if role.population == "department" and not role.department:
                raise LoadError(f"{role.role_id} needs a department.")
        elif role.population or role.department:
            raise LoadError(f"Requestable role {role.role_id} cannot set a population rule.")
        if role.role_id in warehouse.roles:
            raise LoadError(f"Duplicate role id {role.role_id}.")
        warehouse.roles[role.role_id] = role


def _load_assignments(warehouse: Warehouse, payload: dict[str, Any]) -> None:
    rows = payload.get("assignments")
    if not isinstance(rows, list):
        raise LoadError("assignments.yaml needs an assignments list.")
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise LoadError("Each assignment must be a mapping.")
        person_id = _require_str(row, "person_id", "assignments.yaml")
        role_id = _require_str(row, "role_id", "assignments.yaml")
        granted_at = _require_str(row, "granted_at", "assignments.yaml")
        key = (person_id, role_id)
        if key in seen:
            raise LoadError(f"Duplicate assignment {person_id} {role_id}.")
        seen.add(key)
        warehouse.assignments.append(
            RoleAssignment(
                person_id=person_id,
                role_id=role_id,
                source="request",
                granted_at=granted_at,
            )
        )


def _load_accounts(warehouse: Warehouse, payload: dict[str, Any]) -> None:
    rows = payload.get("accounts")
    if not isinstance(rows, list):
        raise LoadError("accounts.yaml needs an accounts list.")
    for row in rows:
        if not isinstance(row, dict):
            raise LoadError("Each pinned account must be a mapping.")
        entitlements = row.get("entitlements", [])
        if not isinstance(entitlements, list):
            raise LoadError(f"Account {row.get('account_id')} entitlements must be a list.")
        account = Account(
            account_id=_require_str(row, "account_id", "accounts.yaml"),
            app_id=_require_str(row, "app_id", "accounts.yaml"),
            person_id=_optional_str(row.get("person_id")),
            username=_require_str(row, "username", "accounts.yaml"),
            status=_require_str(row, "status", "accounts.yaml"),
            entitlements=sorted(str(item) for item in entitlements),
            origin=_require_str(row, "origin", "accounts.yaml"),
        )
        if account.origin not in ACCOUNT_ORIGINS:
            raise LoadError(f"{account.account_id} has unsupported origin.")
        if account.origin == "orphan" and account.person_id is not None:
            raise LoadError(f"Orphan account {account.account_id} cannot have a person_id.")
        if account.origin == "legacy" and account.person_id is None:
            raise LoadError(f"Legacy account {account.account_id} needs a person_id.")
        if account.status not in {"active", "disabled"}:
            raise LoadError(f"{account.account_id} has unsupported status.")
        if account.status == "active" and not account.entitlements:
            raise LoadError(f"Active account {account.account_id} needs entitlements.")
        if account.account_id in warehouse.accounts:
            raise LoadError(f"Duplicate account id {account.account_id}.")
        warehouse.accounts[account.account_id] = account


def _validate_graph(warehouse: Warehouse) -> None:
    for person in warehouse.people.values():
        if person.manager_id is None:
            continue
        if person.manager_id not in warehouse.people:
            raise LoadError(f"{person.person_id} manager {person.manager_id} does not exist.")
        if person.manager_id == person.person_id:
            raise LoadError(f"{person.person_id} cannot be their own manager.")
    for person in warehouse.people.values():
        seen: set[str] = set()
        current = person.manager_id
        while current:
            if current in seen:
                raise LoadError(f"Manager cycle involving {current}.")
            seen.add(current)
            current = warehouse.people[current].manager_id
    for app in warehouse.apps.values():
        if app.owner_person_id not in warehouse.people:
            raise LoadError(f"{app.app_id} owner {app.owner_person_id} does not exist.")
    for role in warehouse.roles.values():
        for grant in role.grants:
            app = warehouse.apps.get(grant.app_id)
            if app is None:
                raise LoadError(f"{role.role_id} grants unknown app {grant.app_id}.")
            if grant.entitlement_id not in app.entitlements:
                raise LoadError(
                    f"{role.role_id} grants unknown entitlement {grant.entitlement_id}."
                )
    for assignment in warehouse.assignments:
        if assignment.person_id not in warehouse.people:
            raise LoadError(f"Assignment person {assignment.person_id} does not exist.")
        role = warehouse.roles.get(assignment.role_id)
        if role is None:
            raise LoadError(f"Assignment role {assignment.role_id} does not exist.")
        if role.kind != "requestable":
            raise LoadError(
                f"Assignment {assignment.person_id} {assignment.role_id} must be requestable. "
                "Birthright roles are computed from the catalog."
            )
        if warehouse.people[assignment.person_id].status != "active":
            raise LoadError(f"Assignment for terminated person {assignment.person_id}.")
    for account in warehouse.accounts.values():
        app = warehouse.apps.get(account.app_id)
        if app is None:
            raise LoadError(f"{account.account_id} uses unknown app {account.app_id}.")
        unknown = set(account.entitlements) - set(app.entitlements)
        if unknown:
            raise LoadError(f"{account.account_id} has unknown entitlements {sorted(unknown)}.")
        if account.person_id is not None and account.person_id not in warehouse.people:
            raise LoadError(f"{account.account_id} links unknown person {account.person_id}.")


def _validate_joiner_person(person: dict[str, Any], event_id: str) -> None:
    missing = JOINER_FIELDS - set(person)
    if missing:
        raise LoadError(f"Joiner {event_id} is missing {sorted(missing)}.")
    if person.get("person_type") not in PERSON_TYPES:
        raise LoadError(f"Joiner {event_id} has an unsupported person_type.")
    status = person.get("status", "active")
    if status != "active":
        raise LoadError(f"Joiner {event_id} must be active.")
    email = person.get("email")
    if not isinstance(email, str) or "@" not in email:
        raise LoadError(f"Joiner {event_id} needs an email address.")


def _read_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise LoadError(f"Missing data file {path}.")
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise LoadError(f"{path} must contain a mapping.")
    return payload


def _require_str(row: dict[str, Any], field: str, source: str) -> str:
    value = row.get(field)
    text = _coerce_str(value)
    if text is None or text == "":
        raise LoadError(f"{source} field {field} is required.")
    return text


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return _coerce_str(value)


def _coerce_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    raise LoadError(f"Expected a string value, got {value!r}.")
