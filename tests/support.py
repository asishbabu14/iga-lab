"""Small in-memory labs for unit tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from iga_lab.clock import ManualClock
from iga_lab.connectors import ConnectorRouter, FileConnector, ScimConnector
from iga_lab.models import Account, App, Grant, Person, Role, RoleAssignment, Warehouse
from iga_lab.publish import Publisher
from iga_lab.scim_server import create_app


@dataclass
class Lab:
    warehouse: Warehouse
    publisher: Publisher
    clock: ManualClock
    client: SyncAsgiClient
    file_root: Path


def make_lab(tmp_path: Path) -> Lab:
    """Three people, email, wiki, and ledger. Nothing is provisioned yet."""
    warehouse = Warehouse()
    warehouse.people["p-1"] = _person("p-1", "Dana Cho", "people-ops", None)
    warehouse.people["p-2"] = _person("p-2", "Nia Cole", "finance", "p-1")
    warehouse.people["p-3"] = _person("p-3", "Lee Park", "engineering", "p-1")
    warehouse.apps["app-email"] = _app("app-email", "MOCK:scim", ["mailbox"], "p-1")
    warehouse.apps["app-wiki"] = _app("app-wiki", "MOCK:scim", ["reader", "editor"], "p-1")
    warehouse.apps["app-ledger"] = _app(
        "app-ledger",
        "MOCK:file",
        ["ledger-read", "journal-entry", "journal-approve"],
        "p-1",
    )
    warehouse.roles["role-staff-email"] = Role(
        role_id="role-staff-email",
        name="Staff email",
        kind="birthright",
        description="Mailbox for every active person.",
        grants=[Grant("app-email", "mailbox")],
        population="all_active",
    )
    warehouse.roles["role-eng-wiki-editor"] = Role(
        role_id="role-eng-wiki-editor",
        name="Engineering wiki editor",
        kind="birthright",
        description="Editors in engineering.",
        grants=[Grant("app-wiki", "editor")],
        population="department",
        department="engineering",
    )
    warehouse.roles["role-finance-ledger-read"] = Role(
        role_id="role-finance-ledger-read",
        name="Finance ledger read",
        kind="birthright",
        description="Ledger read for finance.",
        grants=[Grant("app-ledger", "ledger-read")],
        population="department",
        department="finance",
    )
    warehouse.roles["role-finance-clerk"] = Role(
        role_id="role-finance-clerk",
        name="Finance clerk",
        kind="requestable",
        description="Enter journals.",
        grants=[Grant("app-ledger", "journal-entry")],
    )
    warehouse.roles["role-finance-approver"] = Role(
        role_id="role-finance-approver",
        name="Finance approver",
        kind="requestable",
        description="Approve journals.",
        grants=[Grant("app-ledger", "journal-approve")],
    )
    file_root = tmp_path / "filedrop"
    client = SyncAsgiClient(create_app())
    router = ConnectorRouter(
        {
            "MOCK:scim": ScimConnector(client),
            "MOCK:file": FileConnector(file_root),
        }
    )
    clock = ManualClock()
    return Lab(
        warehouse=warehouse,
        publisher=Publisher(warehouse, router, clock),
        clock=clock,
        client=client,
        file_root=file_root,
    )


def assign(warehouse: Warehouse, person_id: str, role_id: str) -> None:
    warehouse.assignments.append(
        RoleAssignment(
            person_id=person_id,
            role_id=role_id,
            source="request",
            granted_at="2026-01-01T00:00:00Z",
        )
    )


def _person(person_id: str, name: str, department: str, manager_id: str | None) -> Person:
    uid = ".".join(part.lower() for part in name.split())
    return Person(
        person_id=person_id,
        display_name=name,
        person_type="employee",
        status="active",
        department=department,
        title="Analyst",
        email=f"{uid}@example.test",
        uid=uid,
        manager_id=manager_id,
        start_date="2024-01-01",
    )


def _app(app_id: str, connector: str, entitlements: list[str], owner: str) -> App:
    return App(
        app_id=app_id,
        name=app_id,
        owner_person_id=owner,
        connector=connector,
        entitlements=entitlements,
    )


class SyncAsgiClient:
    """Sync facade over the async ASGI transport used by httpx."""

    def __init__(self, app: object) -> None:
        self._app = app

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._request("PUT", url, **kwargs)

    def close(self) -> None:
        return None

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def _go() -> httpx.Response:
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://mock-scim"
            ) as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(_go())


def orphan(warehouse: Warehouse) -> Account:
    account = Account(
        account_id="acct-orphan-wiki",
        app_id="app-wiki",
        person_id=None,
        username="orphan.wiki.legacy",
        status="active",
        entitlements=["reader"],
        origin="orphan",
    )
    warehouse.accounts[account.account_id] = account
    return account
