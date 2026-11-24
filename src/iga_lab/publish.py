"""Publish warehouse account state through the mock connectors."""

from __future__ import annotations

from dataclasses import dataclass, field

from iga_lab.connectors.base import ConnectorRouter
from iga_lab.errors import ConnectorError
from iga_lab.models import Account, ProvisioningResult, Warehouse
from iga_lab.roles import project_role_accounts


@dataclass
class Snapshot:
    """Last account state that connectors accepted."""

    accounts: dict[str, Account] = field(default_factory=dict)

    @classmethod
    def capture(cls, warehouse: Warehouse) -> Snapshot:
        return cls(
            accounts={
                account_id: account.clone() for account_id, account in warehouse.accounts.items()
            }
        )


class Publisher:
    """Project roles to accounts, then write the delta to connectors."""

    def __init__(self, warehouse: Warehouse, router: ConnectorRouter, clock: object) -> None:
        self.warehouse = warehouse
        self.router = router
        self.clock = clock
        self.snapshot = Snapshot()
        self._seq = 0
        self._started = False

    def publish(self) -> list[ProvisioningResult]:
        """Push accounts that differ from the last successful write."""
        project_role_accounts(self.warehouse)
        previous = self.snapshot if self._started else None
        batch: list[ProvisioningResult] = []
        succeeded: dict[str, Account] = {}
        if previous is not None:
            succeeded = {
                account_id: account.clone() for account_id, account in previous.accounts.items()
            }
        for account in sorted(self.warehouse.accounts.values(), key=lambda item: item.account_id):
            prev = None if previous is None else previous.accounts.get(account.account_id)
            if prev is not None and _same(prev, account):
                continue
            results = self._push(account, prev)
            batch.extend(results)
            if results and all(item.success for item in results):
                succeeded[account.account_id] = account.clone()
            elif previous is not None and account.account_id in previous.accounts:
                succeeded[account.account_id] = previous.accounts[account.account_id].clone()
            else:
                succeeded.pop(account.account_id, None)
        self.snapshot = Snapshot(accounts=succeeded)
        self._started = True
        self.warehouse.provisioning_results.extend(batch)
        return batch

    def _push(self, account: Account, prev: Account | None) -> list[ProvisioningResult]:
        app = self.warehouse.apps[account.app_id]
        timestamp = self.clock.iso()  # type: ignore[attr-defined]
        error: str | None = None
        try:
            connector = self.router.for_app(app)
            connector.write(
                app=app,
                accounts=list(self.warehouse.accounts.values()),
                account=account,
                updated_at=timestamp,
            )
        except ConnectorError as exc:
            error = str(exc)
            connector_label = app.connector
        else:
            connector_label = connector.label
        actions = _describe(prev, account)
        results: list[ProvisioningResult] = []
        for action, entitlement_id, detail in actions:
            self._seq += 1
            results.append(
                ProvisioningResult(
                    result_id=f"prov-{self._seq:04d}",
                    timestamp=timestamp,
                    connector=connector_label,
                    action=action,
                    app_id=account.app_id,
                    account_id=account.account_id,
                    person_id=account.person_id,
                    entitlement_id=entitlement_id,
                    success=error is None,
                    detail=error or detail,
                )
            )
        return results


def _same(left: Account, right: Account) -> bool:
    return (
        left.status == right.status
        and list(left.entitlements) == list(right.entitlements)
        and left.username == right.username
        and left.person_id == right.person_id
        and left.origin == right.origin
    )


def _describe(prev: Account | None, account: Account) -> list[tuple[str, str | None, str]]:
    if prev is None:
        state = "active" if account.status == "active" else "disabled"
        entitlements = ",".join(account.entitlements) or "(none)"
        return [("provision", None, f"initial desired state {state}; entitlements {entitlements}")]
    if prev.status == "active" and account.status == "disabled":
        return [("revoke", None, "account disabled and entitlements cleared")]
    removed = sorted(set(prev.entitlements) - set(account.entitlements))
    added = sorted(set(account.entitlements) - set(prev.entitlements))
    actions: list[tuple[str, str | None, str]] = []
    for entitlement in removed:
        actions.append(("revoke", entitlement, f"entitlement {entitlement} removed"))
    for entitlement in added:
        actions.append(("provision", entitlement, f"entitlement {entitlement} granted"))
    if not actions and prev.status != account.status:
        actions.append(("update", None, f"status {prev.status} -> {account.status}"))
    if not actions:
        actions.append(("update", None, "account attributes updated"))
    return actions
