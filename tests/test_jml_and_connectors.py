"""Lifecycle happy paths, feed failures, and both mock connectors."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from iga_lab.connectors import ConnectorRouter, FileConnector, ScimConnector
from iga_lab.errors import ConnectorError, FeedError
from iga_lab.jml import apply_feed
from iga_lab.models import Account, App, HrEvent
from iga_lab.roles import sync_birthright
from iga_lab.scim_server import ScimServer, create_app
from tests.support import assign, make_lab


def test_birthright_and_mover_and_leaver(tmp_path: Path) -> None:
    lab = make_lab(tmp_path)
    notes = sync_birthright(lab.warehouse, lab.clock.iso())
    assert "grant p-3 role-eng-wiki-editor" in notes
    assert "grant p-2 role-finance-ledger-read" in notes
    assert "grant p-1 role-eng-wiki-editor" not in notes
    again = sync_birthright(lab.warehouse, lab.clock.iso())
    assert again == []
    lab.publisher.publish()

    mover = HrEvent(
        event_id="move-1",
        event_type="mover",
        effective_at="2026-02-10T00:00:00Z",
        person_id="p-3",
        changes={"department": "finance", "title": "Revenue Analyst", "manager_id": "p-2"},
    )
    result = apply_feed(lab.warehouse, [mover], lab.publisher, lab.clock)[0]
    roles = {item.role_id for item in lab.warehouse.assignments if item.person_id == "p-3"}
    assert "role-eng-wiki-editor" not in roles
    assert "role-finance-ledger-read" in roles
    assert "role-staff-email" in roles
    assert any(note.startswith("revoke p-3") for note in result.notes)
    wiki = lab.client.get("/scim/v2/Users/acct-app-wiki-p-3")
    assert wiki.status_code == 200
    assert wiki.json()["active"] is False

    leaver = HrEvent(
        event_id="leave-1",
        event_type="leaver",
        effective_at="2026-02-16T00:00:00Z",
        person_id="p-3",
        end_date="2026-02-16",
    )
    apply_feed(lab.warehouse, [leaver], lab.publisher, lab.clock)
    assert lab.warehouse.people["p-3"].status == "terminated"
    assert lab.warehouse.accounts["acct-app-email-p-3"].status == "disabled"
    email = lab.client.get("/scim/v2/Users/acct-app-email-p-3").json()
    assert email["active"] is False
    assert email["mockTarget"] == "MOCK:scim"
    ledger = (lab.file_root / "app-ledger.csv").read_text(encoding="utf-8")
    assert "MOCK:file" in ledger
    assert "|" in ledger
    disabled = [line for line in ledger.splitlines() if "acct-app-ledger-p-3" in line][0]
    assert "disabled" in disabled


def test_joiner_provisions_birthright(tmp_path: Path) -> None:
    lab = make_lab(tmp_path)
    sync_birthright(lab.warehouse, lab.clock.iso())
    lab.publisher.publish()
    event = HrEvent(
        event_id="join-1",
        event_type="joiner",
        effective_at="2026-02-02T00:00:00Z",
        person={
            "person_id": "p-9",
            "display_name": "Maren Cho",
            "person_type": "contractor",
            "department": "engineering",
            "title": "Software Contractor",
            "email": "maren.cho@example.test",
            "uid": "maren.cho",
            "manager_id": "p-1",
            "start_date": "2026-02-02",
        },
    )
    apply_feed(lab.warehouse, [event], lab.publisher, lab.clock)
    roles = {item.role_id for item in lab.warehouse.assignments if item.person_id == "p-9"}
    assert "role-staff-email" in roles
    assert "role-eng-wiki-editor" in roles
    body = lab.client.get("/scim/v2/Users/acct-app-email-p-9").json()
    assert body["active"] is True
    assert body["entitlements"] == ["mailbox"]


def test_feed_failure_paths(tmp_path: Path) -> None:
    lab = make_lab(tmp_path)
    sync_birthright(lab.warehouse, lab.clock.iso())
    lab.publisher.publish()
    with pytest.raises(FeedError, match="does not exist"):
        apply_feed(
            lab.warehouse,
            [
                HrEvent(
                    event_id="m",
                    event_type="mover",
                    effective_at="2026-01-01T00:00:00Z",
                    person_id="missing",
                    changes={"title": "Analyst"},
                )
            ],
            lab.publisher,
            lab.clock,
        )
    with pytest.raises(FeedError, match="does not exist"):
        apply_feed(
            lab.warehouse,
            [
                HrEvent(
                    event_id="l",
                    event_type="leaver",
                    effective_at="2026-01-01T00:00:00Z",
                    person_id="missing",
                    end_date="2026-01-01",
                )
            ],
            lab.publisher,
            lab.clock,
        )
    with pytest.raises(FeedError, match="missing"):
        apply_feed(
            lab.warehouse,
            [
                HrEvent(
                    event_id="j",
                    event_type="joiner",
                    effective_at="2026-01-01T00:00:00Z",
                    person={"person_id": "p-8", "display_name": "Ada"},
                )
            ],
            lab.publisher,
            lab.clock,
        )
    joiner = HrEvent(
        event_id="j2",
        event_type="joiner",
        effective_at="2026-01-01T00:00:00Z",
        person={
            "person_id": "p-8",
            "display_name": "Ada West",
            "person_type": "employee",
            "department": "finance",
            "title": "Analyst",
            "email": "ada.west@example.test",
            "uid": "ada.west",
            "manager_id": "missing-manager",
            "start_date": "2026-01-01",
        },
    )
    with pytest.raises(FeedError, match="manager"):
        apply_feed(lab.warehouse, [joiner], lab.publisher, lab.clock)
    joiner.person["manager_id"] = "p-1"  # type: ignore[index]
    apply_feed(lab.warehouse, [joiner], lab.publisher, lab.clock)
    with pytest.raises(FeedError, match="already exists"):
        apply_feed(
            lab.warehouse,
            [
                HrEvent(
                    event_id="j3",
                    event_type="joiner",
                    effective_at="2026-01-02T00:00:00Z",
                    person=dict(joiner.person or {}),
                )
            ],
            lab.publisher,
            lab.clock,
        )
    leaver = HrEvent(
        event_id="l2",
        event_type="leaver",
        effective_at="2026-03-01T00:00:00Z",
        person_id="p-8",
        end_date="2026-03-01",
    )
    apply_feed(lab.warehouse, [leaver], lab.publisher, lab.clock)
    with pytest.raises(FeedError, match="already terminated"):
        apply_feed(
            lab.warehouse,
            [
                HrEvent(
                    event_id="l3",
                    event_type="leaver",
                    effective_at="2026-03-02T00:00:00Z",
                    person_id="p-8",
                    end_date="2026-03-02",
                )
            ],
            lab.publisher,
            lab.clock,
        )
    with pytest.raises(FeedError, match="Duplicate"):
        apply_feed(
            lab.warehouse,
            [
                HrEvent(
                    event_id="same",
                    event_type="mover",
                    effective_at="2026-01-01T00:00:00Z",
                    person_id="p-2",
                    changes={"title": "Analyst"},
                ),
                HrEvent(
                    event_id="same",
                    event_type="mover",
                    effective_at="2026-01-02T00:00:00Z",
                    person_id="p-2",
                    changes={"title": "Senior Analyst"},
                ),
            ],
            lab.publisher,
            lab.clock,
        )


def test_file_connector_rejects_a_file_path(tmp_path: Path) -> None:
    target = tmp_path / "not-a-directory"
    target.write_text("nope", encoding="utf-8")
    with pytest.raises(ConnectorError, match="not a directory"):
        FileConnector(target)


def test_unknown_connector_router(tmp_path: Path) -> None:
    lab = make_lab(tmp_path)
    app = App(
        app_id="app-other",
        name="Other",
        owner_person_id="p-1",
        connector="MOCK:other",
        entitlements=["read"],
    )
    with pytest.raises(ConnectorError, match="No adapter"):
        lab.publisher.router.for_app(app)


def test_scim_http_failure_is_connector_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={"detail": "nope"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://mock-scim")
    connector = ScimConnector(client)
    account = Account(
        account_id="acct-1",
        app_id="app-email",
        person_id="p-1",
        username="dana.cho",
        status="active",
        entitlements=["mailbox"],
        origin="role",
    )
    app = App(
        app_id="app-email",
        name="Staff email",
        owner_person_id="p-1",
        connector="MOCK:scim",
        entitlements=["mailbox"],
    )
    with pytest.raises(ConnectorError, match="HTTP 500"):
        connector.write(app, [account], account, "2026-02-20T15:00:00Z")


def test_failed_publish_is_recorded_and_retried(tmp_path: Path) -> None:
    lab = make_lab(tmp_path)

    class Boom:
        label = "MOCK:scim"

        def write(self, app, accounts, account, updated_at) -> None:
            del app, accounts, account, updated_at
            raise ConnectorError("MOCK:scim HTTP 500")

    class Remember:
        label = "MOCK:scim"

        def __init__(self) -> None:
            self.written: list[str] = []

        def write(self, app, accounts, account, updated_at) -> None:
            del app, accounts, updated_at
            self.written.append(account.account_id)

    file_connector = FileConnector(lab.file_root)
    lab.publisher.router = ConnectorRouter({"MOCK:scim": Boom(), "MOCK:file": file_connector})
    sync_birthright(lab.warehouse, lab.clock.iso())
    first = lab.publisher.publish()
    assert any(not item.success and item.connector == "MOCK:scim" for item in first)
    assert any(item.success and item.connector == "MOCK:file" for item in first)
    assert "acct-app-ledger-p-2" in (lab.file_root / "app-ledger.csv").read_text(encoding="utf-8")
    remember = Remember()
    lab.publisher.router = ConnectorRouter({"MOCK:scim": remember, "MOCK:file": file_connector})
    second = lab.publisher.publish()
    assert second
    assert all(item.success for item in second)
    assert "acct-app-email-p-1" in remember.written


def test_live_scim_server_roundtrip() -> None:
    server = ScimServer()
    server.start()
    client = server.client()
    try:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["target"] == "MOCK:scim"
        created = client.post(
            "/scim/v2/Users",
            json={"id": "u-1", "userName": "ada.west", "active": True},
        )
        assert created.status_code == 201
        assert created.json()["mockTarget"] == "MOCK:scim"
        fetched = client.get("/scim/v2/Users/u-1")
        assert fetched.json()["userName"] == "ada.west"
        replaced = client.put(
            "/scim/v2/Users/u-1",
            json={"userName": "ada.west", "active": False, "entitlements": []},
        )
        assert replaced.status_code == 200
        assert replaced.json()["active"] is False
        missing = client.get("/scim/v2/Users/missing")
        assert missing.status_code == 404
        rejected = client.post("/scim/v2/Users", json={"id": "u-2"})
        assert rejected.status_code == 400
    finally:
        client.close()
        server.stop()


def test_asgi_service_lists_users() -> None:
    from tests.support import SyncAsgiClient

    client = SyncAsgiClient(create_app())
    client.post("/scim/v2/Users", json={"id": "u-9", "userName": "lee", "appId": "app-wiki"})
    listed = client.get("/scim/v2/Users", params={"appId": "app-wiki"})
    assert listed.json()["totalResults"] == 1
    assert listed.json()["mockTarget"] == "MOCK:scim"


def test_terminated_person_gets_no_birthright(tmp_path: Path) -> None:
    lab = make_lab(tmp_path)
    lab.warehouse.people["p-3"].status = "terminated"
    lab.warehouse.people["p-3"].end_date = "2025-01-01"
    sync_birthright(lab.warehouse, lab.clock.iso())
    roles = {item.role_id for item in lab.warehouse.assignments if item.person_id == "p-3"}
    assert roles == set()
    assign(lab.warehouse, "p-2", "role-finance-clerk")
    assert any(item.person_id == "p-2" for item in lab.warehouse.assignments)
