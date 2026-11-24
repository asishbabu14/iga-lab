"""MOCK:scim connector. Speaks a small SCIM-like JSON dialect over HTTP."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from iga_lab.errors import ConnectorError
from iga_lab.models import Account, App


class ScimTransport(Protocol):
    """The few HTTP calls the connector makes. A real client or a test double."""

    def get(self, url: str, **kwargs: Any) -> Any:
        """GET one user."""

    def post(self, url: str, **kwargs: Any) -> Any:
        """Create one user."""

    def put(self, url: str, **kwargs: Any) -> Any:
        """Replace one user."""


class ScimConnector:
    """Desired-state writer for the in-process MOCK:scim service."""

    label = "MOCK:scim"

    def __init__(self, client: ScimTransport) -> None:
        self.client = client

    def write(
        self,
        app: App,
        accounts: list[Account],
        account: Account,
        updated_at: str,
    ) -> None:
        del accounts
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "id": account.account_id,
            "userName": account.username,
            "active": account.status == "active",
            "externalId": account.person_id or "",
            "displayName": account.username,
            "entitlements": list(account.entitlements),
            "appId": app.app_id,
            "updatedAt": updated_at,
            "mockTarget": "MOCK:scim",
        }
        url = f"/scim/v2/Users/{account.account_id}"
        try:
            existing = self.client.get(url)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"MOCK:scim GET failed for {account.account_id}: {exc}") from exc
        try:
            if existing.status_code == 404:
                response = self.client.post("/scim/v2/Users", json=payload)
            elif existing.status_code >= 400:
                raise ConnectorError(
                    f"MOCK:scim GET failed with HTTP {existing.status_code} "
                    f"for {account.account_id}."
                )
            else:
                response = self.client.put(url, json=payload)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"MOCK:scim write failed for {account.account_id}: {exc}") from exc
        if response.status_code >= 400:
            raise ConnectorError(
                f"MOCK:scim write failed with HTTP {response.status_code} for {account.account_id}."
            )
