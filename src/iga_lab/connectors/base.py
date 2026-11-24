"""Connector protocol shared by the mock targets."""

from __future__ import annotations

from typing import Protocol

from iga_lab.errors import ConnectorError
from iga_lab.models import Account, App


class ProvisioningConnector(Protocol):
    """Write desired account state to one mock target."""

    label: str

    def write(
        self,
        app: App,
        accounts: list[Account],
        account: Account,
        updated_at: str,
    ) -> None:
        """Persist the desired state of one account."""


class ConnectorRouter:
    """Pick the adapter declared on the application record."""

    def __init__(self, connectors: dict[str, ProvisioningConnector]) -> None:
        self.connectors = connectors

    def for_app(self, app: App) -> ProvisioningConnector:
        try:
            return self.connectors[app.connector]
        except KeyError as exc:
            raise ConnectorError(
                f"No adapter for connector {app.connector} on {app.app_id}."
            ) from exc
