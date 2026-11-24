"""Audit evidence pack: JSON, Markdown, and a self-contained HTML report."""

from __future__ import annotations

import html
import json
from pathlib import Path

from iga_lab.campaigns import Campaign
from iga_lab.models import Warehouse
from iga_lab.sod import SodPolicy, SodReport

LAB_NOTICE = (
    "Personal lab project. Synthetic data only. Not a commercial IGA product. "
    "Not affiliated with any employer. Not SOX compliance. "
    "Practice material for SOX-style access-control evidence."
)

EXCEPTION_PATH = (
    "An exception suppresses a violation only when its status is active, the person "
    "currently holds both sides of that policy, and the expiry is still in the future "
    "on the lab clock. Expired and inactive exceptions stay in the report and do not "
    "suppress the finding."
)


def build_evidence(
    *,
    warehouse: Warehouse,
    generated_at: str,
    birthright_notes: list[str],
    jml_results: list[object],
    campaigns: list[Campaign],
    policies: list[SodPolicy],
    sod_before: SodReport,
    sod_after: SodReport,
    lineage: list[str],
) -> dict[str, object]:
    """Assemble the evidence document from warehouse state."""
    before = sod_before.to_dict(warehouse)
    after = sod_after.to_dict(warehouse)
    before_keys = {
        (item["policy_id"], item["person_id"])
        for item in before["violations"]  # type: ignore[index]
    }
    after_keys = {
        (item["policy_id"], item["person_id"])
        for item in after["violations"]  # type: ignore[index]
    }
    cleared = [
        item
        for item in before["violations"]  # type: ignore[index]
        if (item["policy_id"], item["person_id"]) not in after_keys
    ]
    people = list(warehouse.people.values())
    active = [person for person in people if person.status == "active"]
    return {
        "notice": LAB_NOTICE,
        "generated_at": generated_at,
        "lineage": lineage,
        "population": {
            "people": len(people),
            "active": len(active),
            "terminated": len(people) - len(active),
            "employees": sum(person.person_type == "employee" for person in people),
            "contractors": sum(person.person_type == "contractor" for person in people),
        },
        "apps": [
            {
                "app_id": app.app_id,
                "name": app.name,
                "connector": app.connector,
                "owner_person_id": app.owner_person_id,
                "entitlements": list(app.entitlements),
            }
            for app in sorted(warehouse.apps.values(), key=lambda item: item.app_id)
        ],
        "birthright_notes": list(birthright_notes),
        "joiner_mover_leaver": [item.to_dict() for item in jml_results],  # type: ignore[attr-defined]
        "current_access": _current_access(warehouse),
        "orphan_accounts": _orphans(warehouse),
        "campaigns": [campaign.to_dict(warehouse) for campaign in campaigns],
        "provisioning_results": [result.to_dict() for result in warehouse.provisioning_results],
        "sod": {
            "exception_path": EXCEPTION_PATH,
            "policies": [policy.to_dict() for policy in policies],
            "before_campaigns": before,
            "after_campaigns": after,
            "cleared_by_certification": cleared,
            "open_before": sum(
                item["status"] == "open"
                for item in before["violations"]  # type: ignore[index]
            ),
            "open_after": sum(
                item["status"] == "open"
                for item in after["violations"]  # type: ignore[index]
            ),
        },
        "summary": {
            "provisioning_results": len(warehouse.provisioning_results),
            "provisioning_failures": sum(
                not result.success for result in warehouse.provisioning_results
            ),
            "campaigns": len(campaigns),
            "violations_cleared": len(cleared),
            "before_violation_keys": len(before_keys),
        },
    }


def write_evidence(evidence: dict[str, object], report_dir: Path) -> dict[str, Path]:
    """Write JSON, Markdown, and HTML. The HTML file has no external assets."""
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "evidence.json"
    markdown_path = report_dir / "evidence.md"
    html_path = report_dir / "evidence.html"
    json_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(evidence), encoding="utf-8")
    html_path.write_text(render_html(evidence), encoding="utf-8")
    return {"json": json_path, "markdown": markdown_path, "html": html_path}


def render_markdown(evidence: dict[str, object]) -> str:
    """Render a readable evidence memo."""
    population = evidence["population"]
    summary = evidence["summary"]
    sod = evidence["sod"]
    lines = [
        "# iga-lab access evidence",
        "",
        evidence["notice"],
        "",
        f"Generated at: {evidence['generated_at']}",
        "",
        "## Population",
        "",
        (
            f"People {population['people']}, active {population['active']}, "
            f"terminated {population['terminated']}, employees {population['employees']}, "
            f"contractors {population['contractors']}."
        ),
        "",
        "## Separation of duties",
        "",
        str(sod["exception_path"]),
        "",
        f"Open violations before certification: {sod['open_before']}.",
        f"Open violations after certification: {sod['open_after']}.",
        "",
        "### Before certification",
        "",
        _md_violations(sod["before_campaigns"]["violations"]),
        "",
        "### Exception assessments before certification",
        "",
        _md_assessments(sod["before_campaigns"]["exception_assessments"]),
        "",
        "### Cleared by certification",
        "",
        _md_violations(sod["cleared_by_certification"]),
        "",
        "### After certification",
        "",
        _md_violations(sod["after_campaigns"]["violations"]),
        "",
        "## Certification campaigns",
        "",
    ]
    for campaign in evidence["campaigns"]:
        counts = campaign["counts"]
        lines.extend(
            [
                f"### {campaign['name']}",
                "",
                (
                    f"Id {campaign['campaign_id']}. Kind {campaign['kind']}. "
                    f"Status {campaign['status']}. Opened {campaign['opened_at']}. "
                    f"Closed {campaign['closed_at']}. Due {campaign['due_at']}."
                ),
                (
                    f"Approve {counts.get('approve', 0)}, revoke {counts.get('revoke', 0)}, "
                    f"missed {counts.get('missed', 0)}, pending {counts.get('pending', 0)}."
                ),
                "",
                "Reviewer identity is stored on decided_by. Missed items have no decided_by.",
                "",
                _md_table(
                    [
                        "decision",
                        "subject",
                        "role or entitlement",
                        "reviewer",
                        "decided_by",
                        "decided_at",
                        "comment",
                    ],
                    [
                        [
                            item["decision"],
                            _subject(item),
                            item.get("role_id") or item.get("entitlement_id"),
                            item.get("reviewer_id"),
                            item.get("decided_by") or "",
                            item.get("decided_at") or "",
                            item.get("comment") or "",
                        ]
                        for item in campaign["items"]
                    ],
                ),
                "",
            ]
        )
    lines.extend(
        [
            "## Joiner, mover, leaver",
            "",
            _md_table(
                ["event_id", "type", "person", "effective_at", "summary"],
                [
                    [
                        item["event_id"],
                        item["event_type"],
                        item["person_id"],
                        item["effective_at"],
                        item["summary"],
                    ]
                    for item in evidence["joiner_mover_leaver"]
                ],
            ),
            "",
            "## Provisioning results",
            "",
            (
                f"Results {summary['provisioning_results']}, "
                f"failures {summary['provisioning_failures']}."
            ),
            "",
            _md_table(
                [
                    "result_id",
                    "timestamp",
                    "connector",
                    "action",
                    "app",
                    "account",
                    "entitlement",
                    "success",
                    "detail",
                ],
                [
                    [
                        item["result_id"],
                        item["timestamp"],
                        item["connector"],
                        item["action"],
                        item["app_id"],
                        item["account_id"],
                        item.get("entitlement_id") or "",
                        item["success"],
                        item["detail"],
                    ]
                    for item in evidence["provisioning_results"]
                ],
            ),
            "",
            "## Current access",
            "",
        ]
    )
    for person in evidence["current_access"]:
        role_text = (
            ", ".join(f"{role['role_id']} ({role['source']})" for role in person["roles"])
            or "(none)"
        )
        account_bits = []
        for account in person["accounts"]:
            entitlements = "/".join(account["entitlements"]) or "-"
            account_bits.append(f"{account['app_id']}:{account['status']}:{entitlements}")
        account_text = ", ".join(account_bits) or "(none)"
        lines.append(
            f"- {person['person_id']} {person['display_name']} | {person['person_type']} | "
            f"{person['department']} | {person['status']} | roles {role_text} | "
            f"accounts {account_text}"
        )
    lines.extend(["", "### Orphan and unlinked accounts", ""])
    if evidence["orphan_accounts"]:
        for account in evidence["orphan_accounts"]:
            lines.append(
                f"- {account['account_id']} {account['username']} {account['app_id']} "
                f"{account['status']} {','.join(account['entitlements']) or '(none)'} "
                f"origin {account['origin']}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Lineage", ""])
    for item in evidence["lineage"]:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def render_html(evidence: dict[str, object]) -> str:
    """Render a single HTML file. Values are escaped. No remote assets."""
    population = evidence["population"]
    summary = evidence["summary"]
    sod = evidence["sod"]
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        "<title>iga-lab access evidence</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        "<header>",
        "<p class='eyebrow'>iga-lab evidence pack</p>",
        "<h1>Access-control evidence</h1>",
        f"<p class='notice'>{esc(evidence['notice'])}</p>",
        f"<p class='meta'>Generated at {esc(evidence['generated_at'])}</p>",
        "</header>",
        "<main>",
        "<section class='cards'>",
        _card("People", population["people"]),
        _card("Active", population["active"]),
        _card("Open SoD after", sod["open_after"]),
        _card("Provisioning failures", summary["provisioning_failures"]),
        "</section>",
        "<section>",
        "<h2>Separation of duties</h2>",
        f"<p>{esc(sod['exception_path'])}</p>",
        f"<p>Open before certification: {esc(sod['open_before'])}. "
        f"Open after certification: {esc(sod['open_after'])}.</p>",
        "<h3>Before certification</h3>",
        _html_violations(sod["before_campaigns"]["violations"]),
        "<h3>Exception assessments before certification</h3>",
        _html_assessments(sod["before_campaigns"]["exception_assessments"]),
        "<h3>Cleared by certification</h3>",
        _html_violations(sod["cleared_by_certification"]),
        "<h3>After certification</h3>",
        _html_violations(sod["after_campaigns"]["violations"]),
        "</section>",
        "<section>",
        "<h2>Certification campaigns</h2>",
        "<p>Reviewer identity is stored on decided_by. A missed review has no decided_by.</p>",
    ]
    for campaign in evidence["campaigns"]:
        counts = campaign["counts"]
        parts.append(f"<h3>{esc(campaign['name'])}</h3>")
        parts.append(
            "<p>"
            f"Id {esc(campaign['campaign_id'])}. Kind {esc(campaign['kind'])}. "
            f"Status {esc(campaign['status'])}. Opened {esc(campaign['opened_at'])}. "
            f"Closed {esc(campaign['closed_at'])}. Due {esc(campaign['due_at'])}. "
            f"Approve {esc(counts.get('approve', 0))}, revoke {esc(counts.get('revoke', 0))}, "
            f"missed {esc(counts.get('missed', 0))}."
            "</p>"
        )
        parts.append(
            _table(
                [
                    "decision",
                    "subject",
                    "role or entitlement",
                    "reviewer",
                    "decided_by",
                    "decided_at",
                    "comment",
                ],
                [
                    [
                        item["decision"],
                        _subject(item),
                        item.get("role_id") or item.get("entitlement_id"),
                        item.get("reviewer_id"),
                        item.get("decided_by") or "",
                        item.get("decided_at") or "",
                        item.get("comment") or "",
                    ]
                    for item in campaign["items"]
                ],
            )
        )
    parts.extend(
        [
            "</section>",
            "<section>",
            "<h2>Joiner, mover, leaver</h2>",
            _table(
                ["event_id", "type", "person", "effective_at", "summary"],
                [
                    [
                        item["event_id"],
                        item["event_type"],
                        item["person_id"],
                        item["effective_at"],
                        item["summary"],
                    ]
                    for item in evidence["joiner_mover_leaver"]
                ],
            ),
            "</section>",
            "<section>",
            "<h2>Provisioning results</h2>",
            (
                f"<p>Results {esc(summary['provisioning_results'])}, "
                f"failures {esc(summary['provisioning_failures'])}.</p>"
            ),
            _table(
                [
                    "result_id",
                    "timestamp",
                    "connector",
                    "action",
                    "app",
                    "account",
                    "entitlement",
                    "success",
                    "detail",
                ],
                [
                    [
                        item["result_id"],
                        item["timestamp"],
                        item["connector"],
                        item["action"],
                        item["app_id"],
                        item["account_id"],
                        item.get("entitlement_id") or "",
                        item["success"],
                        item["detail"],
                    ]
                    for item in evidence["provisioning_results"]
                ],
            ),
            "</section>",
            "<section>",
            "<h2>Current access</h2>",
            _table(
                ["person", "type", "department", "status", "roles", "accounts"],
                [
                    [
                        f"{person['person_id']} {person['display_name']}",
                        person["person_type"],
                        person["department"],
                        person["status"],
                        ", ".join(role["role_id"] for role in person["roles"]),
                        "; ".join(
                            f"{account['app_id']} {account['status']} "
                            f"{'/'.join(account['entitlements'])}"
                            for account in person["accounts"]
                        ),
                    ]
                    for person in evidence["current_access"]
                ],
            ),
            "<h3>Orphan and unlinked accounts</h3>",
            _table(
                ["account", "username", "app", "status", "entitlements", "origin"],
                [
                    [
                        account["account_id"],
                        account["username"],
                        account["app_id"],
                        account["status"],
                        ",".join(account["entitlements"]),
                        account["origin"],
                    ]
                    for account in evidence["orphan_accounts"]
                ],
            ),
            "</section>",
            "</main>",
            f"<footer>{esc(evidence['notice'])}</footer>",
            "</body>",
            "</html>",
            "",
        ]
    )
    return "\n".join(parts)


def _current_access(warehouse: Warehouse) -> list[dict[str, object]]:
    rows = []
    for person in sorted(warehouse.people.values(), key=lambda item: item.person_id):
        accounts = [
            {
                "account_id": account.account_id,
                "app_id": account.app_id,
                "username": account.username,
                "status": account.status,
                "entitlements": list(account.entitlements),
                "origin": account.origin,
            }
            for account in sorted(warehouse.accounts.values(), key=lambda item: item.account_id)
            if account.person_id == person.person_id
        ]
        roles = [
            {
                "role_id": assignment.role_id,
                "name": warehouse.roles[assignment.role_id].name,
                "source": assignment.source,
            }
            for assignment in sorted(
                warehouse.assignments,
                key=lambda item: (item.person_id, item.role_id),
            )
            if assignment.person_id == person.person_id
        ]
        rows.append(
            {
                "person_id": person.person_id,
                "display_name": person.display_name,
                "person_type": person.person_type,
                "status": person.status,
                "department": person.department,
                "title": person.title,
                "email": person.email,
                "manager_id": person.manager_id,
                "roles": roles,
                "accounts": accounts,
            }
        )
    return rows


def _orphans(warehouse: Warehouse) -> list[dict[str, object]]:
    return [
        {
            "account_id": account.account_id,
            "app_id": account.app_id,
            "username": account.username,
            "status": account.status,
            "entitlements": list(account.entitlements),
            "origin": account.origin,
            "person_id": account.person_id,
        }
        for account in sorted(warehouse.accounts.values(), key=lambda item: item.account_id)
        if account.person_id is None
    ]


def _subject(item: dict[str, object]) -> str:
    if item.get("subject_person_id"):
        name = item.get("subject_name") or ""
        return f"{item['subject_person_id']} {name}".strip()
    return str(item.get("account_id") or "")


def _md_violations(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "_None._"
    return _md_table(
        ["status", "policy", "person", "left", "right", "exception"],
        [
            [
                row["status"],
                row["policy_id"],
                f"{row['person_id']} {row.get('person_name') or ''}".strip(),
                f"{row['left']['app_id']}:{row['left']['entitlement_id']}",
                f"{row['right']['app_id']}:{row['right']['entitlement_id']}",
                row.get("exception_id") or "",
            ]
            for row in rows
        ],
    )


def _md_assessments(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "_None._"
    return _md_table(
        ["exception", "policy", "person", "approver", "outcome", "expires_at", "reason"],
        [
            [
                row["exception_id"],
                row["policy_id"],
                row["person_id"],
                row["approver_id"],
                row["outcome"],
                row["expires_at"],
                row["reason"],
            ]
            for row in rows
        ],
    )


def _html_violations(rows: list[dict[str, object]]) -> str:
    return _table(
        ["status", "policy", "person", "left", "right", "exception"],
        [
            [
                row["status"],
                row["policy_id"],
                f"{row['person_id']} {row.get('person_name') or ''}".strip(),
                f"{row['left']['app_id']}:{row['left']['entitlement_id']}",
                f"{row['right']['app_id']}:{row['right']['entitlement_id']}",
                row.get("exception_id") or "",
            ]
            for row in rows
        ],
    )


def _html_assessments(rows: list[dict[str, object]]) -> str:
    return _table(
        ["exception", "policy", "person", "approver", "outcome", "expires_at", "reason"],
        [
            [
                row["exception_id"],
                row["policy_id"],
                row["person_id"],
                row["approver_id"],
                row["outcome"],
                row["expires_at"],
                row["reason"],
            ]
            for row in rows
        ],
    )


def _md_table(headers: list[str], rows: list[list[object]]) -> str:
    head = "| " + " | ".join(headers) + " |"
    rule = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(_md_cell(cell) for cell in row) + " |" for row in rows]
    return "\n".join([head, rule, *body])


def _md_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "/").replace("\n", " ")


def _table(headers: list[str], rows: list[list[object]]) -> str:
    head = "".join(f"<th>{esc(header)}</th>" for header in headers)
    body = []
    for row in rows:
        cells = "".join(f"<td>{esc(cell)}</td>" for cell in row)
        body.append(f"<tr>{cells}</tr>")
    return "<table><thead><tr>" + head + "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"


def _card(label: str, value: object) -> str:
    return f"<article><p>{esc(label)}</p><strong>{esc(value)}</strong></article>"


def esc(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


_CSS = """
:root { color-scheme: light; }
body {
  margin: 0;
  font: 16px/1.45 Georgia, "Iowan Old Style", serif;
  color: #1b1914;
  background: #f7f4ee;
}
header, main, footer { max-width: 1100px; margin: 0 auto; padding: 1.25rem 1.5rem; }
header { background: #16352f; color: #f7f4ee; max-width: none; }
header h1, header p { max-width: 1100px; margin-left: auto; margin-right: auto; }
h1 { font-size: 2rem; margin: 0.2rem 0 0.6rem; }
h2 { margin-top: 2rem; }
h3 { margin-bottom: 0.4rem; }
.eyebrow {
  letter-spacing: 0.08em;
  text-transform: uppercase;
  font: 0.75rem sans-serif;
  margin: 0;
}
.notice, footer { background: #efe6d4; color: #1b1914; }
header .notice { background: transparent; color: #f7f4ee; }
.meta { font: 0.95rem sans-serif; }
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 0.75rem;
  padding-top: 1rem;
}
.cards article { background: white; border: 1px solid #d9d2c5; padding: 0.75rem 1rem; }
.cards p { margin: 0; font: 0.8rem sans-serif; color: #4d463c; }
.cards strong { font-size: 1.6rem; }
table {
  width: 100%;
  border-collapse: collapse;
  background: white;
  font: 0.82rem/1.35 sans-serif;
  margin: 0.5rem 0 1.25rem;
}
th, td {
  border: 1px solid #d9d2c5;
  padding: 0.35rem 0.45rem;
  text-align: left;
  vertical-align: top;
}
th { background: #efe6d4; }
footer { font: 0.9rem sans-serif; }
"""
