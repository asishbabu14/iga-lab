"""Domain records for the identity warehouse."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Person:
    person_id: str
    display_name: str
    person_type: str
    status: str
    department: str
    title: str
    email: str
    uid: str
    manager_id: str | None
    start_date: str
    end_date: str | None = None


@dataclass
class App:
    app_id: str
    name: str
    owner_person_id: str
    connector: str
    entitlements: list[str]
    description: str = ""


@dataclass(frozen=True)
class Grant:
    app_id: str
    entitlement_id: str


@dataclass
class Role:
    role_id: str
    name: str
    kind: str
    description: str
    grants: list[Grant]
    population: str | None = None
    department: str | None = None


@dataclass
class RoleAssignment:
    person_id: str
    role_id: str
    source: str
    granted_at: str


@dataclass
class Account:
    account_id: str
    app_id: str
    person_id: str | None
    username: str
    status: str
    entitlements: list[str]
    origin: str

    def clone(self) -> Account:
        return Account(
            account_id=self.account_id,
            app_id=self.app_id,
            person_id=self.person_id,
            username=self.username,
            status=self.status,
            entitlements=list(self.entitlements),
            origin=self.origin,
        )


@dataclass
class ProvisioningResult:
    result_id: str
    timestamp: str
    connector: str
    action: str
    app_id: str
    account_id: str
    person_id: str | None
    entitlement_id: str | None
    success: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "result_id": self.result_id,
            "timestamp": self.timestamp,
            "connector": self.connector,
            "action": self.action,
            "app_id": self.app_id,
            "account_id": self.account_id,
            "person_id": self.person_id,
            "entitlement_id": self.entitlement_id,
            "success": self.success,
            "detail": self.detail,
        }


@dataclass
class Warehouse:
    """In-memory identity warehouse. All records are synthetic."""

    people: dict[str, Person] = field(default_factory=dict)
    apps: dict[str, App] = field(default_factory=dict)
    roles: dict[str, Role] = field(default_factory=dict)
    assignments: list[RoleAssignment] = field(default_factory=list)
    accounts: dict[str, Account] = field(default_factory=dict)
    provisioning_results: list[ProvisioningResult] = field(default_factory=list)

    def require_person(self, person_id: str) -> Person:
        try:
            return self.people[person_id]
        except KeyError as exc:
            raise KeyError(f"Unknown person {person_id}") from exc

    def role_account(self, person_id: str, app_id: str) -> Account | None:
        for account in self.accounts.values():
            if (
                account.origin == "role"
                and account.person_id == person_id
                and account.app_id == app_id
            ):
                return account
        return None


@dataclass
class HrEvent:
    event_id: str
    event_type: str
    effective_at: str
    person_id: str | None = None
    person: dict[str, object] | None = None
    changes: dict[str, object] | None = None
    end_date: str | None = None


@dataclass
class JmlResult:
    event_id: str
    event_type: str
    person_id: str
    effective_at: str
    summary: str
    notes: list[str]
    provisioning: list[ProvisioningResult]

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "person_id": self.person_id,
            "effective_at": self.effective_at,
            "summary": self.summary,
            "notes": list(self.notes),
            "provisioning_result_ids": [item.result_id for item in self.provisioning],
        }
