"""MOCK:file connector. Writes a pipe-delimited file per application."""

from __future__ import annotations

import csv
import io
from pathlib import Path

from iga_lab.errors import ConnectorError
from iga_lab.models import Account, App

README_TEXT = (
    "MOCK:file\n"
    "Synthetic delimited-file target for the iga-lab personal project.\n"
    "Not a live system.\n"
)


class FileConnector:
    """Desired-state writer for the delimited-file target."""

    label = "MOCK:file"

    def __init__(self, root: Path) -> None:
        if root.exists() and not root.is_dir():
            raise ConnectorError(f"MOCK:file root {root} is not a directory.")
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        (root / "README.txt").write_text(README_TEXT, encoding="utf-8")

    def write(
        self,
        app: App,
        accounts: list[Account],
        account: Account,
        updated_at: str,
    ) -> None:
        del account
        rows = [item for item in accounts if item.app_id == app.app_id]
        path = self.root / f"{app.app_id}.csv"
        payload = _render(rows, updated_at)
        temporary = path.with_suffix(".csv.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)

    def read(self, app_id: str) -> str:
        path = self.root / f"{app_id}.csv"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")


def _render(accounts: list[Account], updated_at: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="|", lineterminator="\n")
    writer.writerow(
        [
            "account_id",
            "person_id",
            "username",
            "status",
            "entitlements",
            "updated_at",
            "mock_target",
        ]
    )
    for account in sorted(accounts, key=lambda item: item.account_id):
        writer.writerow(
            [
                account.account_id,
                account.person_id or "",
                account.username,
                account.status,
                ";".join(account.entitlements),
                updated_at,
                "MOCK:file",
            ]
        )
    return buffer.getvalue()
